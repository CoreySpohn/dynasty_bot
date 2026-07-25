# NathanPeterman Bot 🏈

A Dynasty Fantasy Football Discord Bot for the Superflexers league.

## Quick Start

```bash
# Install dependencies
uv sync

# Set up environment variables (copy from .env.example)
cp .env.example .env
# Edit .env with your tokens

# Run the bot
uv run python main.py
```

## Commands Reference

### 🎲 Kohl's Cash (Playoff Betting)

| Command | Description |
|---------|-------------|
| `/kohls balance` | Check your KC balance |
| `/kohls bet <team> <amount>` | Bet on a team (spread) |
| `/kohls propbet <id> <amount>` | Bet on a prop |
| `/kohls props` | View available prop bets |
| `/kohls mybets` | See your active bets |
| `/kohls leaderboard` | See who's winning |
| `/kohls store` | Browse purchasable perks |
| `/kohls buy <item>` | Buy store items |

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/kohls fetchgames` | Fetch playoff games from The Odds API |
| `/kohls fetchprops` | Fetch player props |
| `/kohls games` | List current games |
| `/kohls resolve <game_id> <winner>` | Manually resolve a game |
| `/kohls give <user> <amount>` | Give KC to a user |

---

### 🗞️ League Rumors

| Command | Description |
|---------|-------------|
| **DM the bot** | Send a rumor to be rewritten and posted |
| `/rumor <text>` | Submit a rumor (picks reporter) |
| `/listreporters` | See available reporter personalities |

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/randomrumor [category] [context]` | Force post a random rumor, optionally scoped to trade/draft/drama/general or your own freeform direction |
| `/nflrumor <text>` | Post NFL news to dedicated channel |

---

### 🚕 Taxi Squad Raiding

| Command | Description |
|---------|-------------|
| `/raid <player_name>` | Raid a player from someone's taxi |
| `/raidhistory [season]` | View raid history |
| `/taxisquad [team]` | View taxi squads |

**How it works:**
- Cost = Draft round + (round - 1) in picks
- Round 3 player = 2nd + 3rd round picks
- UDFA = 4th round pick
- Victim gets tagged and reminded daily until resolved

---

### 📊 Analytics

| Command | Description |
|---------|-------------|
| `/standings` | League standings with points |
| `/rankings` | Power rankings image (max potential points, win %, avg points, dynasty value) |
| `/matchups [week]` | Matchups for a week |
| `/roster <team>` | View a team's roster |
| `/schedule [week]` | View matchups |
| `/seasonreport` | End-of-season summary |
| `/payouts` | Payout breakdown |
| `/draftorder` | Current projected draft order |
| `/primetime` | Primetime lineup lock status |

---

### 💰 Trade Values

Dynasty trade values are synced daily from KeepTradeCut and stored historically so trends can be tracked over time. Team dynasty value also factors into `/rankings` (see Analytics above).

The same daily job also snapshots **roster composition**, because Sleeper only ever serves *current* rosters — who owned whom on a past date is unrecoverable once the day passes. Those two dated tables together (`ktc_values` = what a player was worth on a date, `roster_snapshots` = who owned them) are what make `/valuehistory` and `/tradegrades` possible.

| Command | Description |
|---------|-------------|
| `/tradevalue <player>` | Look up a player's current 1QB/Superflex value, rank, and 7-day trend |
| `/teamvalues` | Rank owners by total dynasty roster value |
| `/valuemovers` | Winners & losers: biggest team value swings over the last 7 days (market movement on **currently owned** players only) |
| `/valuehistory [days]` | Team value now vs N days ago, priced against the roster each team actually had then — so **trades count** |
| `/tradecalc <side_a> <side_b>` | Compare KTC value of two sides of a proposed trade — players *and* picks, comma-separated |
| `/tradegrades [week]` | Grade a week's completed trades using values from the day they happened, plus how they've aged |

**Picks in `/tradecalc`:** KTC prices rookie picks by tier (`2027 Early 1st`). Loose phrasings resolve automatically — `2027 1st`, `27 2nd round pick`, `2028 late 3rd` — and a pick with no stated tier is priced as **Mid**. Only rounds 1–4 are published by KTC.

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/synctradevalues` | Manually refresh trade values from KeepTradeCut and snapshot rosters |

---

### 🎭 Random Responses

| Command | Description |
|---------|-------------|
| `/proposeresponse <text> [chance]` | Propose a bot response (needs 6 votes) |
| `/listresponses` | See all random responses |

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/addresponse <text> [chance]` | Force-add a response |
| `/removeresponse <text>` | Remove a response |

---

### 📈 Draft & Trades

| Command | Description |
|---------|-------------|
| `/pickcalc <picks...>` | Calculate trade value |
| `/roster value <team>` | See team's pick values |

Trade polls are automatically created from Sleeper trades.

---

### 🏷️ Nickname Tags

Tags owner nicknames with league context (e.g. `Corey [3rd place]`) without touching whatever nickname they already have. Standings rank syncs automatically once a day during the season; live "on the clock" tracking polls Sleeper every 5 minutes whenever an actual draft is in progress.

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/syncstandingsnicknames` | Manually tag every owner with their current standings rank |
| `/syncdraftnicknames` | Tag every owner with their rookie draft pick slot (e.g. `Pick 3`) |
| `/clearnicknametags` | Remove all bot-applied nickname tags |

---

## Configuration Files

| File | Purpose |
|------|---------|
| `config/members.yaml` | League member identities (Discord, Sleeper, nicknames) |
| `config/reporters.yaml` | Reporter personalities for rumors |
| `config/rumor_tables.yaml` | Random tables for auto-rumor generation |
| `config/deadlines.yaml` | League deadline reminders |
| `config/responses.yaml` | Random bot responses |
| `config/pick_values.yaml` | Draft pick trade values |

---

## Environment Variables

```bash
# Required
DISCORD_TOKEN=your_bot_token
SLEEPER_LEAGUE_ID=your_league_id

# For Kohl's Cash betting
THE_ODDS_API_KEY=your_odds_api_key

# For AI rumors - any subset works. The bot rotates between every backend
# that has a key (different models write in noticeably different voices) and
# fails over to another one if the first is rate limited or down.
GEMINI_API_KEY=your_gemini_key
ANTHROPIC_API_KEY=your_anthropic_key
OPENAI_API_KEY=your_openai_key

# Channel IDs
RUMORS_CHANNEL_ID=channel_for_rumors
NFL_CHANNEL_ID=channel_for_nfl_news
ALERT_CHANNEL_ID=channel_for_alerts
KOHLS_FORUM_CHANNEL_ID=forum_for_game_threads
```

---

## Features Overview

### 🎰 Kohl's Cash System
- Bet on NFL playoff games (spreads & totals)
- Player prop bets (QB/RB yards, etc.)
- Store to buy perks (custom colors, nicknames, targeted responses)
- Transaction ledger for full audit trail

### 🗞️ AI-Powered Rumors
- 16+ reporter personalities (Morgan Freeman, Stephen A. Smith, etc.)
- Table-based rumor generation with REAL roster players
- Submit via DM or slash command
- Separate NFL news channel
- Rotates across Gemini / Claude / OpenAI for stylistic variety, with automatic failover between backends

### 💰 Dynasty Value Tracking
- Daily KeepTradeCut sync, stored as dated snapshots (KTC has no historical API, so this is the only record)
- Daily roster composition snapshots, since Sleeper only serves current rosters
- Team value over time *including* trades, not just market movement
- Trade grading at trade-date values, plus how each side has aged

### 🚕 Taxi Raiding
- Full draft origin lookup across seasons
- Automatic cost calculation
- Discord tagging for victim
- 24-hour reminder loop

### 📊 Sleeper Integration
- Live roster data
- Trade poll creation
- Standings and schedules
- Player lookup

### ⏰ Scheduled Reminders
- Deadline notifications
- Ice Chug monitoring (IR players blocking starters)
- Primetime lineup locks

---

## Development

```bash
# Run with hot reload
uv run python main.py

# Run the test suite (also runs in CI on every push and PR)
uv run pytest
```

### What gets stored vs. computed live

The rule for this codebase: **persist only what upstream won't give back.**

| Source | Retains history? | So we... |
|--------|------------------|----------|
| Sleeper (matchups, results, transactions, brackets) | Yes, forever, chained via `previous_league_id` | compute live — don't duplicate it |
| KeepTradeCut | No API, no history | snapshot daily (`ktc_values`) |
| Roster composition | Sleeper serves *current* only | snapshot daily (`roster_snapshots`) |
| Bot-generated (raids, Kohl's ledger, nickname tags) | Nothing else knows it | persist |

Storing derived Sleeper data means keeping a second source of truth in sync for no gain, and it can always be recomputed. Storing KTC values and roster composition is the opposite: miss a day and it's gone permanently.

### Project Structure
```
dynasty_bot/
├── main.py                   # Bot entrypoint
├── config.py                 # Environment config
├── database.py               # SQLite schema
├── cogs/                     # Discord command modules
│   ├── kohls.py              # Kohl's Cash betting
│   ├── rumors.py             # AI rumors
│   ├── taxi.py               # Taxi raiding
│   ├── trade_values.py       # KTC values, team value history, trade grading
│   ├── analytics.py          # Power rankings, standings, matchups
│   ├── responses.py          # Random responses
│   ├── scheduler.py          # Reminders
│   └── ...
├── clients/                  # External API clients
│   ├── sleeper.py            # Sleeper API
│   ├── keeptradecut.py       # KeepTradeCut dynasty values
│   ├── nfl_schedule.py       # NFL schedule (nflreadpy)
│   └── odds.py               # The Odds API
├── lib/                      # Shared utilities
│   ├── members.py            # Member registry
│   ├── roster_history.py     # Daily roster composition snapshots
│   ├── standings.py          # Standings computation
│   ├── plotting.py           # Table/chart rendering
│   ├── nicknames.py          # Nickname tagging
│   ├── ai_client.py          # Gemini
│   ├── claude_client.py      # Claude
│   └── openai_client.py      # OpenAI
└── config/                   # YAML configurations
```

---

## Credits

Built for the Superflexers Dynasty League 🏆

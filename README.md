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
| `/roster <team>` | View a team's roster |
| `/schedule [week]` | View matchups |

---

### 💰 Trade Values

Dynasty trade values are synced daily from KeepTradeCut and stored historically so trends can be tracked over time.

| Command | Description |
|---------|-------------|
| `/tradevalue <player>` | Look up a player's current 1QB/Superflex value, rank, and 7-day trend |

**Admin Commands:**
| Command | Description |
|---------|-------------|
| `/synctradevalues` | Manually refresh trade values from KeepTradeCut |

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

# For AI rumors
GEMINI_API_KEY=your_gemini_key

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

# Run specific command tests
uv run python -c "from cogs.kohls import KohlsCash"
```

### Project Structure
```
dynasty_bot/
├── main.py              # Bot entrypoint
├── config.py            # Environment config
├── database.py          # SQLite schema
├── cogs/                # Discord command modules
│   ├── kohls.py         # Kohl's Cash betting
│   ├── rumors.py        # AI rumors
│   ├── taxi.py          # Taxi raiding
│   ├── responses.py     # Random responses
│   ├── scheduler.py     # Reminders
│   └── ...
├── clients/             # External API clients
│   ├── sleeper.py       # Sleeper API
│   └── odds.py          # The Odds API
├── lib/                 # Shared utilities
│   ├── members.py       # Member registry
│   └── ai_client.py     # Gemini AI
└── config/              # YAML configurations
```

---

## Credits

Built for the Superflexers Dynasty League 🏆

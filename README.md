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

**Unprompted rumor cadence:** roughly **2 per week**, tuned via `RUMORS_PER_WEEK` in `cogs/rumors.py`. The loop ticks hourly and rolls small per-tick odds, with an 18-hour minimum gap and no posting between midnight and 8am **league time** (`America/New_York`, not the server's clock).

Hourly-with-low-odds rather than one coarse timer, deliberately: a `tasks.loop(hours=48)` fires at the same clock time forever — whenever the bot last restarted — so if that time falls inside the quiet window, *every* tick is skipped and nothing ever posts. That is exactly what used to happen.

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
| `/luckindex` | Record vs. what all-play scoring deserved, plus lineup efficiency |

---

### 🏆 Weekly Recaps

Awards and the shame wall **auto-post** to the announcements channel once per completed week. `posted_recaps` records what's been posted, so a restart or a repeated tick never double-posts. The slash commands below run them on demand for any week.

| Command | Description |
|---------|-------------|
| `/awards [week]` | Highest scorer, biggest upset, best bench, worst beat |
| `/shamewall [week]` | Losses the optimal lineup would have won, points left on the bench, zero-point starters |

Both default to the **last completed week** — the in-progress week has partial scores, and crowning a highest scorer mid-Sunday is noise.

**How "biggest upset" is decided:** the bot doesn't make predictions, so it uses season-to-date average points as the expectation — the winner who beat the team averaging the most more per week.

---

### 🔮 Projections

One model (`lib/projections.py`) powers all four commands, so they can't disagree with each other. Each team's weekly score is treated as normal, with mean and spread **shrunk toward the league average** by a weight that decays as real games accumulate — dynasty roster value supplies the prior, since it's the only signal available in week 1. A matchup is then the difference of two normals; season odds come from simulating the remaining schedule 10,000 times.

| Command | Description |
|---------|-------------|
| `/predictions [week]` | Matchup predictions with confidence %, graded once the week finishes |
| `/predictionrecord` | Season accuracy, including whether stated confidence is calibrated |
| `/playoffodds` | Simulated playoff odds, expected wins, and mean seed |
| `/sacko` | Last-place odds — the race for the toilet bowl |

**Predictions are recorded before kickoff and never rewritten.** That's the whole point of the `predictions` table: a prediction only means something if it was locked in beforehand, so it can't be recomputed after the fact. `INSERT OR IGNORE` (not upsert) enforces it — a re-run can't quietly improve the bot's past opinions. Asking for a week with nothing recorded projects it live and says so, and those don't count toward the record.

**Measured accuracy:** backtested on the 2025 regular season the model went **46/78 (59.0%)** with a mean stated confidence of 61.8% — so slightly overconfident, and about 9 points better than a coin flip. On 78 games that's only p≈0.06 one-sided, i.e. one season short of statistically meaningful. Deliberately left untuned rather than fitted to a single season.

**`clinched` and `eliminated` mean mathematically certain**, not "no simulation found it." They're only set when few enough games remain to enumerate every outcome (2ⁿ, n ≤ 14); beyond that the flags stay off and the embed says so, because 10,000 misses is not the same as impossible.

---

### 📜 League History

All-time records across every season Sleeper has on record, chained through `previous_league_id`. Attribution is by **owner**, not roster: a roster ID is only stable within one season's league, so aggregating by roster would credit the wrong person after a league renewal.

| Command | Description |
|---------|-------------|
| `/h2h [owner] [against]` | All-time head-to-head — one owner vs. everyone, or a specific pair |
| `/rings` | Championship counts with 🏆 per title, from Sleeper's winners bracket |

These walk every season, so they're the most API-expensive commands in the bot. Fine occasionally; not for a loop.

---

### 🔥 Trash Talk

| Command | Description |
|---------|-------------|
| `/trashtalk <opponent> [reporter]` | AI smack talk aimed at another owner, in a reporter's voice |

Grounded in real numbers pulled live — their record, points left on the bench, luck score, close-game record, dynasty value rank, and whether they're your opponent this week — because specific lands harder than generic. Prompt-capped to team-and-decisions banter, nothing personal.

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
| Bot-generated (raids, Kohl's ledger, nickname tags, posted recaps, predictions) | Nothing else knows it | persist |

Storing derived Sleeper data means keeping a second source of truth in sync for no gain, and it can always be recomputed. Storing KTC values and roster composition is the opposite: miss a day and it's gone permanently.

**`lib/results.py` is where that recomputation happens** — the single place Sleeper matchup payloads get turned into per-team weekly results (score, opponent, W/L, optimal lineup, points left on the bench). Power rankings, awards, the shame wall, the luck index, head-to-head and `/trashtalk` all read from it, so their numbers agree by construction instead of by coincidence. Completed weeks are immutable and cached in-process; the current week never is.

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
│   ├── recaps.py             # Weekly awards, shame wall, luck index
│   ├── projections.py        # Predictions, playoff odds, sacko watch
│   ├── history.py            # All-time head-to-head, championship rings
│   ├── responses.py          # Random responses
│   ├── scheduler.py          # Reminders
│   └── ...
├── clients/                  # External API clients
│   ├── sleeper.py            # Sleeper API
│   ├── keeptradecut.py       # KeepTradeCut dynasty values
│   ├── nfl_schedule.py       # NFL schedule (nflreadpy)
│   └── odds.py               # The Odds API
├── lib/                      # Shared utilities
│   ├── results.py            # Weekly results derivation (the shared layer)
│   ├── projections.py        # Scoring model, win probability, simulation
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

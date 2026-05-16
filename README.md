# ow-stats

[![Docker Image](https://github.com/bennerrrr/ow-stats/actions/workflows/docker-image.yml/badge.svg)](https://github.com/bennerrrr/ow-stats/actions/workflows/docker-image.yml) [![Latest release](https://img.shields.io/github/v/release/bennerrrr/ow-stats)](https://github.com/bennerrrr/ow-stats/releases) [![Last commit](https://img.shields.io/github/last-commit/bennerrrr/ow-stats)](https://github.com/bennerrrr/ow-stats/commits/main) [![CodeQL Advanced](https://github.com/bennerrrr/ow-stats/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/bennerrrr/ow-stats/actions/workflows/codeql.yml)

A self-hosted stats tracker for **Overwatch 2** and **Hell Let Loose**. Periodically polls player data from public APIs, stores historical snapshots in SQLite, and serves a web dashboard with charts. Optionally pushes session summaries and rank-change alerts to Discord.

## Features

- Track multiple players across Overwatch 2 and Hell Let Loose from a single dashboard
- Automatic periodic polling on a configurable interval, with smart deduplication (snapshots only written when stats change or after 24 hours)
- Per-player detail pages with Chart.js charts, rank history, and play session breakdowns
- Session detection — Discord alerts fire once a play session ends, not mid-session
- Discord bot with slash commands for adding/removing players, looking up stats, and managing notification channels
- Admin utility panel at `/utils` for DB export, import, vacuum, health checks, and on-demand polling

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async), APScheduler, discord.py
- **Frontend:** Jinja2 templates, Tailwind CSS, Chart.js
- **Database:** SQLite via aiosqlite
- **External APIs:** [OverFast API](https://overfast-api.tekrop.fr) (OW2), Steam Web API (HLL)

## Setup

### Docker (recommended)

The image is published to GitHub Container Registry on every merge to `main` and supports `linux/amd64` and `linux/arm64` (Raspberry Pi, Apple Silicon via Rosetta, etc.).

```bash
cp .env.example .env
# Edit .env with your values

docker compose pull   # fetch the latest image from ghcr.io
docker compose up -d
```

To update to the latest version later:

```bash
docker compose pull && docker compose up -d
```

> **First run / existing data migration:** the compose file uses a named Docker volume (`ow_stats_data`) for the SQLite database. If you have an existing `./data/ow_stats.db`, copy it into the volume before starting:
> ```bash
> docker run --rm -v $(pwd)/data:/src -v ow_stats_data:/dst alpine cp -a /src/. /dst/
> ```

### Building from source

```bash
cp .env.example .env
# Edit .env with your values

docker compose build
docker compose up -d
```

### Local

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your values

uvicorn main:app --host 0.0.0.0 --port 8000
```

The dashboard is available at `http://localhost:8000`.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRACKED_PLAYERS` | No | — | Comma-separated players to seed on first startup (see format below) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./data/ow_stats.db` | SQLite connection string |
| `POLL_INTERVAL_MINUTES` | No | `30` | How often to fetch stats for all players |
| `DISPLAY_TIMEZONE` | No | `America/New_York` | IANA timezone used for timestamps in the web UI |
| `DISCORD_BOT_TOKEN` | No | — | Discord bot token; leave blank to disable the bot entirely |
| `STEAM_API_KEY` | HLL only | — | Steam Web API key required for Hell Let Loose players |
| `UTILS_TOKEN` | No | — | Secret token for the `/utils` admin panel; leave blank to disable |

### Player format

```
# Overwatch 2 — use the BattleTag (Username#1234)
TRACKED_PLAYERS=Ben#1234,Alice#5678

# Hell Let Loose — use the 17-digit Steam64 ID, suffixed with :hll
TRACKED_PLAYERS=76561198012345678:hll

# Mixed
TRACKED_PLAYERS=Ben#1234,Alice#5678,76561198012345678:hll
```

Players in `TRACKED_PLAYERS` are seeded into the database on first startup only. Additional players can be added at any time via the web UI or the Discord `/add_player` command.

## Web Dashboard

### Hub (`/`)

Landing page showing the total number of tracked operators for each game, linking to the game-specific pages.

### Game pages (`/overwatch`, `/hll`)

Grid of player cards, each showing the latest snapshot. For OW2 cards: competitive ranks across all four queues, overall win rate and KDA, top hero, and a 7-day trend line (win rate delta and KDA delta). For HLL cards: career kills, playtime, and a 7-day kills/playtime trend. Each card has inline REFRESH and REMOVE actions, plus a link to the full player detail page.

### Player detail (`/players/{battletag}`)

Per-player page with:

- **Rank chart** (OW2 only) — stepped line chart of rank changes over the past 90 days, one line per queue (tank, damage, support, open), with tier-coloured axes
- **Stats chart** — OW2: win rate and KDA over time with dual Y-axes; HLL: career kills and total XP over time
- **Chart filter** — client-side date filter (7d / 30d / 90d / all), defaulting to 30d when data spans more than 30 days
- **Career stats** — OW2: role breakdown (tank/damage/support games, W/L, KDA), top hero table with time played, gamemode tabs (competitive/quickplay); HLL: kill rate, headshot %, sector caps, XP, role breakdown
- **Session history** — OW2: a row per detected play session showing games, wins/losses, WR, KDA delta; HLL: session rows with playtime duration, kills, and XP gained
- **Rank history table** (OW2) — chronological list of distinct rank states over the 90-day window
- **Refresh button** — force-fetches fresh data from the API and updates the page via AJAX without a full reload

## Discord Bot

### Setup

1. Create an application and bot at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Copy the bot token into `DISCORD_BOT_TOKEN` in `.env`
3. Invite the bot to your server with the `bot` and `applications.commands` scopes
4. Use `/set_channel` in any channel to start receiving notifications there

### Slash commands

| Command | Description |
|---|---|
| `/add_player player_id [game]` | Start tracking a player. `player_id` is a BattleTag (`Name#1234`) for OW2 or a Steam64 ID for HLL. `game` defaults to Overwatch 2. Fetches and displays a stats embed on success. |
| `/remove_player player_id` | Stop tracking a player. `player_id` autocompletes from all tracked players. |
| `/stats player_id` | Show a rich stats embed for a tracked player: current ranks, career stats, a 7-day trend (games played, win rate, and KDA deltas), the most recent play session (W/L, session WR, KDA delta, and relative time), and top heroes. Footer shows time since last data update. For OW2, also supports live lookup of any BattleTag even if not tracked (trend and session data are unavailable for untracked players). |
| `/players` | List all currently tracked players grouped by game, each showing time since their last data update. |
| `/set_channel [game]` | Register the current channel to receive notifications. `game` can be "All games" (default), "Overwatch 2 only", or "Hell Let Loose only". Running the command again on an already-registered channel updates the game filter. |
| `/remove_channel` | Unregister the current channel from notifications. |

### Notifications

Notifications are sent as Discord embeds to all registered channels that match the game filter.

**OW2 — Session summary:** fires after a play session ends (detected when `games_played` stops increasing across polls). The embed shows session W/L, any rank changes during the session, and deltas for overall win rate and KDA compared to the session start.

**OW2 — Stats update:** fires when ranks or aggregate stats change between polls with no corresponding increase in `games_played` (e.g. a rank adjustment or API data correction).

**HLL — Session summary:** fires when `playtime_forever` stops increasing after a session. Shows session duration, kills, headshot rate, sector caps, and XP gained during the session.

If the bot is disconnected when a notification is ready, it is queued in memory and flushed automatically once the bot reconnects.

## Admin Utilities (`/utils`)

Navigate to `/utils` in the browser. All actions require the `UTILS_TOKEN` set in `.env`; the token is saved in your browser's `localStorage` so you only need to enter it once.

| Action | Description |
|---|---|
| **System Health** | Shows DB reachability and row counts, scheduler state and job count, Discord bot readiness and latency, and server uptime. Runs automatically on page load if a token is saved. |
| **DB Export** | Downloads a clean, consistent binary snapshot of the SQLite database — safe to run while the app is live. Equivalent to `wget http://host/utils/db/export?token=SECRET`. |
| **DB Import** | Upload a `.db` file to replace the live database. The uploaded file is validated against the expected schema before the swap is made. The scheduler is paused and the connection pool is drained atomically during the switch. |
| **DB Vacuum** | Runs SQLite `VACUUM` to compact the database and reclaim space from deleted rows. Reports before/after file sizes. |
| **Force Poll** | Immediately polls all tracked players outside the normal schedule. Waits for the full poll to complete before returning. |

The same actions are also available as a JSON API:

```bash
# Health
curl "http://localhost:8000/utils/health?token=SECRET"

# Export
wget "http://localhost:8000/utils/db/export?token=SECRET" -O backup.db

# Import
curl -X POST "http://localhost:8000/utils/db/import?token=SECRET" -F "file=@backup.db"

# Vacuum
curl -X POST "http://localhost:8000/utils/db/vacuum?token=SECRET"

# Force poll
curl -X POST "http://localhost:8000/utils/poll?token=SECRET"
```

## External API notes

**Overwatch 2** data is fetched from [OverFast API](https://overfast-api.tekrop.fr), a free community-run API. No API key is required. Players must have their Career Profile set to **Public** in the Overwatch in-game settings — private profiles are skipped with a warning.

**Hell Let Loose** data is pulled from the Steam Web API using the game's stats endpoint. A `STEAM_API_KEY` is required (free at [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey)). The player's Steam profile visibility must be set to **Public** and their **Game details** must also be public, otherwise the poll is skipped.

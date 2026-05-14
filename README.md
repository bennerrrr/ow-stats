# ow-stats

A self-hosted stats tracker for **Overwatch 2** and **Hell Let Loose**. Periodically polls player data, stores historical snapshots, serves a web dashboard with charts, and sends session summaries and rank-change alerts to Discord.

## Features

- Track multiple players across Overwatch 2 and Hell Let Loose
- Automatic periodic polling (configurable interval)
- Web dashboard with per-player stat history and Chart.js visualizations
- Session detection — reports play session summaries after a player goes offline
- Discord bot integration: slash commands, session reports, rank change alerts

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 (async), APScheduler, discord.py
- **Frontend:** Jinja2 templates, Tailwind CSS, Chart.js
- **Database:** SQLite via aiosqlite
- **External APIs:** [OverFast API](https://overfast-api.tekrop.fr) (OW2), Steam Web API (HLL)

## Setup

### Local

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your values

uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Docker

```bash
cp .env.example .env
# Edit .env with your values

docker-compose up -d
```

The dashboard is available at `http://localhost:8000`.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TRACKED_PLAYERS` | Yes | — | Comma-separated player list (see format below) |
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./data/ow_stats.db` | SQLite connection string |
| `POLL_INTERVAL_MINUTES` | No | `30` | How often to fetch stats |
| `DISPLAY_TIMEZONE` | No | `America/New_York` | IANA timezone for the web UI |
| `DISCORD_BOT_TOKEN` | No | — | Discord bot token; leave blank to disable |
| `STEAM_API_KEY` | HLL only | — | Steam Web API key for Hell Let Loose players |

### Player format

```
# Overwatch 2 — use the battletag
TRACKED_PLAYERS=Ben#1234,Alice#5678

# Hell Let Loose — append :hll with the Steam64 ID
TRACKED_PLAYERS=Ben#1234,76561198012345678:hll

# Mixed
TRACKED_PLAYERS=Ben#1234,Alice#5678,76561198012345678:hll
```

Players listed in `TRACKED_PLAYERS` are seeded into the database on first startup. Additional players can be added later via the Discord bot's `/add` slash command or the web UI.

## Discord Bot

To enable Discord notifications, create a bot at [discord.com/developers/applications](https://discord.com/developers/applications), copy the token into `DISCORD_BOT_TOKEN`, and invite the bot to your server with the `bot` and `applications.commands` scopes.

Once the bot is in a server you can use `/setchannel` to designate a channel for notifications.

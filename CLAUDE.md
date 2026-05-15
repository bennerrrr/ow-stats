# CLAUDE.md — ow-stats

Self-hosted stats tracker for **Overwatch 2** and **Hell Let Loose**. Polls player APIs on a configurable interval, stores historical snapshots in SQLite, serves a web dashboard, and pushes session/rank alerts to Discord.

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 async, APScheduler |
| Frontend | Jinja2 templates, Tailwind CSS, Chart.js 4.4 |
| Database | SQLite via aiosqlite (`data/ow_stats.db`, owned by root in prod) |
| External APIs | OverFast API (OW2), Steam Web API (HLL) |
| Discord | discord.py with slash commands |

## File map

```
main.py              — App entry point, lifespan (init_db → seed_players → scheduler → bot)
models.py            — SQLAlchemy ORM: Player, StatSnapshot, DiscordChannel
database.py          — Engine, AsyncSessionLocal, get_db(), init_db() (additive ALTER migrations)
scheduler.py         — APScheduler polling loop, snapshot functions, Discord alert dispatch
ow_client.py         — OverFast API client, 1h cache, returns PlayerData dataclass
hll_client.py        — Steam Web API client, 30m cache, returns HLLPlayerData dataclass
discord_bot.py       — discord.py bot, slash commands, embed builders, notification queue
routers/players.py   — All HTTP routes, snapshot-to-JSON helpers, session/trend computation
                       Key helpers: _build_player_context, _compute_rank_history,
                       _rank_history_to_json, _compute_ow_trend, _compute_hll_trend
                       Custom Jinja2 filters: localdt, urltag, timeago ("LIVE"/"2h ago"/"3d ago")
templates/
  base.html          — Global layout, worldmonitor-style dark theme (#0a0a0a bg, #111111 cards,
                       #272727 borders), SF Mono font stack, status-pulse animation, glow classes
  index.html         — Command hub landing page: SECTOR 01/02 entry cards with player counts
  overwatch.html     — OW2 game page: player grid, add-form, left-orange-border cards,
                       LIVE/stale status dot per card; hides non-standard battletags (no '#')
  hll.html           — HLL game page: player grid, add-form, left-green-border cards
  player.html        — Per-player detail page; command panel + rank chart side-by-side flex row;
                       initRankChart() step-line chart with tier-colored axes; initOWChart/HLLChart;
                       AJAX refresh via XHR header; hides non-standard battletags on header
  partials/
    player_live.html — Swappable stats content (OW2: career stats, role cards from raw_stats.roles,
                       gamemode tabs, rank history table, sessions; HLL: career overview with
                       derived rates, combat/support, role breakdown, sessions)
    stats_panel.html — Reusable hero/stats table with games-played column from raw_stats.heroes
cleanup_dupes.py     — One-shot script to delete pre-deduplication duplicate snapshot rows
```

## Routes

| Method | Path | Template | Description |
|---|---|---|---|
| GET | `/` | `index.html` | Hub landing — player counts only, links to game pages |
| GET | `/overwatch` | `overwatch.html` | OW2 player grid + add-player form |
| GET | `/hll` | `hll.html` | HLL player grid + add-player form |
| GET | `/players/{battletag}` | `player.html` | Per-player detail page |
| POST | `/players/add` | — | Add player; redirects to `/overwatch` or `/hll` |
| POST | `/players/{battletag}/delete` | — | Remove player; redirects to game page |
| POST | `/players/{battletag}/refresh` | partial or redirect | Refresh stats; AJAX returns `player_live.html` fragment |

Add/delete redirects go to the game-specific page (`/overwatch` or `/hll`), not `/`.

## Data model

**`players`** — one row per tracked player  
- `battletag` (unique) — OW battletag (`Name#1234`) or Steam ID (HLL)  
- `game` — `"overwatch"` | `"hell_let_loose"`  
- `display_name`, `avatar_url` — updated on every poll even if no snapshot is saved

**`stat_snapshots`** — time-series history  
- OW columns: `rank_tank/damage/support/open` (strings like `"Gold 3"`), `games_played`, `games_won`, `games_lost`, `kda`, `win_rate` (stored as 0–1 float), `top_heroes` (JSON), `stats_by_gamemode` (JSON), `raw_summary`, `raw_stats`
- HLL column: `game_data` (JSON with `kills`, `headshots`, `playtime_forever`, `total_xp`, `sector_caps`, `role_xp`, etc.)
- `fetched_at` — UTC datetime, no timezone info stored (always treat as UTC)

**`discord_channels`** — guild/channel registrations  
- `game` NULL = receives all games; `"overwatch"` or `"hell_let_loose"` = game-specific

## Snapshot write logic (important)

`_snapshot_ow()` and `_snapshot_hll()` in `scheduler.py` only insert a new `StatSnapshot` if:
- Stats actually changed from the previous snapshot, **OR**
- The previous snapshot is >24 hours old (daily anchor so charts always have coverage)

`player.display_name` and `player.avatar_url` are always updated regardless.

Change detection uses `_ow_ranks_or_stats_changed()` (ranks + games_played + kda + win_rate) for OW and `_hll_stats_changed()` (kills + total_xp + playtime_forever + headshots + sector_caps) for HLL.

**Do not revert to always-inserting** — the DB had 97% duplicate rows before this was added.

## Query patterns

All snapshot queries use **time-based cutoffs, not row count limits**:
- Player detail page: `WHERE fetched_at >= NOW() - 90 days`
- Game pages (`/overwatch`, `/hll`) trend: `WHERE fetched_at >= NOW() - 7 days`
- Refresh AJAX endpoint: same 90-day cutoff as player detail

Never use `.limit(N)` for snapshot queries — with polling every 30 min and even a few duplicates, a count limit translates to an unpredictable time window.

## Snapshot-to-JSON helpers (routers/players.py)

`_snapshots_to_json(snapshots)` — OW chart data, oldest-first, deduplicates consecutive identical `(win_rate, kda, games_played)` tuples (safety net for pre-existing DB dupes). Outputs: `ts`, `date`, `win_rate` (%), `kda`, `games_played`, `comp_win_rate` (%), `qp_win_rate` (%).

`_hll_snapshots_to_json(snapshots)` — HLL chart data, same pattern. Outputs: `ts`, `date`, `kills`, `xp`, `headshots`, `sector_caps`.

Snapshots from the DB are DESC-ordered (newest first); both helpers call `reversed()` internally to iterate oldest-first.

## Session/alert pipeline

After every poll, `_snapshot_ow()` runs session tracking outside the DB session block:
- If `games_played` increased → accumulate into `_pending_sessions[battletag]`
- If `games_played` stopped increasing → fire `_send_ow_report()` (Discord embed with deltas)
- If ranks/stats changed with no new games → fire `_send_stats_update()`

HLL uses the same `_pending_sessions` dict keyed by `steam_id`, detecting sessions via `playtime_forever` delta.

The Discord notification queue (`_notification_queue` in `discord_bot.py`) buffers alerts while the bot is reconnecting.

## Refresh button flow

`POST /players/{battletag}/refresh`:
1. Invalidates the API cache (`ow_client.invalidate_cache` or `hll_client.invalidate_cache`)
2. Calls `snapshot_player(battletag)` — runs in its own `AsyncSessionLocal` session
3. Calls `await db.refresh(player)` — necessary because step 2 may have updated `display_name`/`avatar_url` in a separate session
4. If `X-Requested-With: XMLHttpRequest` → renders `partials/player_live.html` with fresh 90-day snapshot query and returns the HTML fragment
5. Otherwise → `303` redirect to player page

The JS in `player.html` posts with the XHR header, swaps `#live-stats` innerHTML, then re-runs `initChart()` which reads the updated `#snapshot-data` JSON element.

## Charts (Chart.js 4.4, all in player.html)

`initRankChart()` — compact stepped line chart (OW2 only) placed side-by-side with the command panel. Data from `rank_history_json` (chronological rank-change snapshots). Tier-colored y-axis labels, filled area under each line, no legend. Only shown when ≥2 distinct rank states exist in the 90-day window.

`initOWChart()` — line chart, dual Y-axes. Left (`yWR`): overall win rate %, comp win rate %, QP win rate % (dashed). Right (`yKDA`): KDA.

`initHLLChart()` — line chart, dual Y-axes. Left (`yKills`): career kills, headshots (dotted, conditional). Right (`yXP`): total XP (dashed, conditional).

`filterChart(days)` and `filterDataByDays()` do client-side slicing of `allSnapshotData`. Default: 30d if span > 30 days, else All.

## Adding a new game

1. Add a client module (`newgame_client.py`) returning a typed dataclass, with a `_cache` dict and `invalidate_cache()`
2. Add `game_data` fields to `StatSnapshot` or add new columns via `init_db()` ALTER block
3. Add `_snapshot_newgame()` to `scheduler.py` following the change-gate pattern
4. Wire into `snapshot_player()` dispatch and `_pending_sessions` tracking
5. Add JSON helper and context builder in `routers/players.py`; add `GET /newgame` route + `_build_game_page` helper
6. Create `templates/newgame.html` game page (follow `overwatch.html` / `hll.html` pattern)
7. Add template branch in `partials/player_live.html`
8. Add SECTOR card to `index.html` hub

## Common gotchas

- **`fetched_at` has no tzinfo in SQLite** — always `.replace(tzinfo=timezone.utc)` before arithmetic. Both `_tz()` helper and the `_snapshot_*` functions do this.
- **`win_rate` is stored as 0–1** — multiply by 100 before displaying. The OverFast API returns 0–100 and `ow_client.py` normalizes it on ingest.
- **`stats_by_gamemode` win_rate is also 0–1** — same normalization applied by `_parse_stats()`.
- **DB is owned by root in prod** — `cleanup_dupes.py` requires `sudo python3 cleanup_dupes.py`.
- **`snapshot_player()` uses its own session** — after calling it, always `await db.refresh(player)` in the caller's session if you need fresh player data.
- **HLL `battletag` field holds the Steam ID** — the `battletag` column is game-agnostic; for HLL players it stores the Steam64 ID.
- **`player_live.html` is rendered both inside `player.html` and directly by the refresh endpoint** — it must be self-contained and guard against empty `snapshots`.
- **Non-standard OW battletags** — some players are added via internal hash/UUID (no `#` in the string). Templates detect this with `'#' in player.battletag` and hide the raw ID from display; the API still uses it as-is for lookups.
- **OW role stats use `raw_stats.roles`, not `top_heroes` aggregation** — `raw_stats.roles.{tank|damage|support}` has exact game counts, W/L, KDA, deaths, damage per role. The `role_stats` context variable (from `_compute_role_stats`) is still computed but not used for display; `raw_stats.roles` is read directly in the template.
- **`_build_player_context` (OW2) returns `rank_history` + `rank_history_json`** — `rank_history` is newest-first list of dicts (one per distinct rank state); `rank_history_json` is the chronological JSON for the rank chart. Both are empty/`"[]"` when no snapshots exist.

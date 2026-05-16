import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Player, StatSnapshot
from ow_client import fetch_player as ow_fetch_player, ProfilePrivateError, PlayerNotFoundError as OWPlayerNotFoundError, OverFastError, InvalidBattletagError
from hll_client import fetch_player as hll_fetch_player, PlayerNotFoundError as HLLPlayerNotFoundError, HLLClientError

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


def _slog(value: str) -> str:
    return str(value).replace("\n", " ").replace("\r", " ")


_TIER_ORDER = {
    "bronze": 0, "silver": 1, "gold": 2, "platinum": 3,
    "diamond": 4, "master": 5, "grandmaster": 6, "champion": 7,
}
_OW_GAME_MILESTONES = {100, 250, 500, 1000, 2500, 5000}
_HLL_KILL_MILESTONES = {1000, 5000, 10000, 25000, 50000, 100000}


def _rank_tier(rank: str | None) -> tuple[int, int]:
    """(tier_idx, -division) — higher is better in both axes."""
    if not rank:
        return (-1, 0)
    parts = rank.lower().split()
    tier = _TIER_ORDER.get(parts[0], -1)
    div = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return (tier, -div)


def _rank_improved(old: str | None, new: str | None) -> bool:
    return _rank_tier(new) > _rank_tier(old) and _rank_tier(old) != (-1, 0)


def _crossed_milestone(prev: int | None, new: int | None, milestones: set) -> int | None:
    if prev is None or new is None:
        return None
    for m in sorted(milestones):
        if prev < m <= new:
            return m
    return None


def _safe_avatar_url(url: str | None) -> str | None:
    if url and url.startswith("https://") and len(url) <= 500:
        return url
    return None

# battletag -> {baseline, latest, player_name, avatar_url}
_pending_sessions: dict[str, dict] = {}


async def _snapshot_ow(battletag: str) -> None:
    try:
        data = await ow_fetch_player(battletag)
    except InvalidBattletagError:
        logger.warning("Skipping %s — invalid battletag format", _slog(battletag))
        return
    except ProfilePrivateError:
        logger.warning("Skipping %s — profile is private", _slog(battletag))
        return
    except OWPlayerNotFoundError:
        logger.warning("Skipping %s — player not found", _slog(battletag))
        return
    except OverFastError as e:
        logger.error("OverFast API error for %s: %s", _slog(battletag), e)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == battletag))
        player = result.scalar_one_or_none()
        if player is None:
            return

        prev_result = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        prev_snapshot = prev_result.scalar_one_or_none()
        prev_dict = {
            "games_played": prev_snapshot.games_played if prev_snapshot else None,
            "games_won": prev_snapshot.games_won if prev_snapshot else None,
            "rank_tank": prev_snapshot.rank_tank if prev_snapshot else None,
            "rank_damage": prev_snapshot.rank_damage if prev_snapshot else None,
            "rank_support": prev_snapshot.rank_support if prev_snapshot else None,
            "rank_open": prev_snapshot.rank_open if prev_snapshot else None,
            "kda": prev_snapshot.kda if prev_snapshot else None,
            "win_rate": prev_snapshot.win_rate if prev_snapshot else None,
        }

        player.display_name = data.username
        player.avatar_url = _safe_avatar_url(data.avatar)

        new_comparable = {
            "rank_tank": data.rank_tank,
            "rank_damage": data.rank_damage,
            "rank_support": data.rank_support,
            "rank_open": data.rank_open,
            "games_played": data.games_played,
            "kda": data.kda,
            "win_rate": data.win_rate,
        }
        prev_too_old = prev_snapshot is None or (
            datetime.now(timezone.utc) - prev_snapshot.fetched_at.replace(tzinfo=timezone.utc)
        ).total_seconds() > 86400
        should_snapshot = prev_too_old or _ow_ranks_or_stats_changed(prev_dict, new_comparable)

        if should_snapshot:
            snapshot = StatSnapshot(
                player_id=player.id,
                fetched_at=datetime.now(timezone.utc),
                rank_tank=data.rank_tank,
                rank_damage=data.rank_damage,
                rank_support=data.rank_support,
                rank_open=data.rank_open,
                games_played=data.games_played,
                games_won=data.games_won,
                games_lost=data.games_lost,
                kda=data.kda,
                win_rate=data.win_rate,
                top_heroes=[
                    {"hero": h.hero, "name": h.name, "time_played": h.time_played,
                     "win_rate": h.win_rate, "kda": h.kda,
                     "damage_per_10_min": h.damage_per_10_min,
                     "healing_per_10_min": h.healing_per_10_min,
                     "eliminations_per_10_min": h.eliminations_per_10_min}
                    for h in data.top_heroes
                ],
                stats_by_gamemode=data.stats_by_gamemode,
                raw_summary=data.raw_summary,
                raw_stats=data.raw_stats,
            )
            session.add(snapshot)
            fetched_at = snapshot.fetched_at
            logger.info("OW snapshot saved for %s", _slog(battletag))
        else:
            fetched_at = datetime.now(timezone.utc)
            logger.debug("No OW stat change for %s, skipping snapshot", _slog(battletag))

        await session.commit()

    # Session tracking: accumulate deltas across polls and fire the report
    # only once a full poll cycle passes with no new games. This handles the
    # OverFast API delay, which can spread a single play session across several
    # polls before all games appear.
    prev_games = prev_dict["games_played"]
    new_games = data.games_played
    new_dict = {
        "games_played": data.games_played,
        "games_won": data.games_won,
        "games_lost": data.games_lost,
        "kda": data.kda,
        "win_rate": data.win_rate,
        "rank_tank": data.rank_tank,
        "rank_damage": data.rank_damage,
        "rank_support": data.rank_support,
        "rank_open": data.rank_open,
        "fetched_at": fetched_at,
    }

    if prev_games is not None and new_games is not None and new_games > prev_games:
        if battletag not in _pending_sessions:
            _pending_sessions[battletag] = {
                "baseline": prev_dict,
                "latest": new_dict,
                "player_name": data.username,
                "avatar_url": _safe_avatar_url(data.avatar),
            }
        else:
            _pending_sessions[battletag]["latest"] = new_dict
            _pending_sessions[battletag]["player_name"] = data.username
            _pending_sessions[battletag]["avatar_url"] = _safe_avatar_url(data.avatar)
    else:
        if battletag in _pending_sessions:
            sess = _pending_sessions.pop(battletag)
            asyncio.create_task(_send_ow_report(
                player_name=sess["player_name"],
                battletag=battletag,
                avatar_url=sess["avatar_url"],
                prev=sess["baseline"],
                new=sess["latest"],
            ))
        elif _ow_ranks_or_stats_changed(prev_dict, new_dict):
            asyncio.create_task(_send_stats_update(
                player_name=data.username,
                battletag=battletag,
                avatar_url=_safe_avatar_url(data.avatar),
                prev=prev_dict,
                new=new_dict,
            ))

    # Milestone: rank-up (any role improved tier)
    rank_up_msgs = []
    for role, prev_rank, new_rank in [
        ("Tank", prev_dict.get("rank_tank"), data.rank_tank),
        ("Damage", prev_dict.get("rank_damage"), data.rank_damage),
        ("Support", prev_dict.get("rank_support"), data.rank_support),
        ("Open Queue", prev_dict.get("rank_open"), data.rank_open),
    ]:
        if _rank_improved(prev_rank, new_rank):
            rank_up_msgs.append(f"{role}: {prev_rank} → **{new_rank}**")
    if rank_up_msgs:
        asyncio.create_task(_send_milestone(
            player_name=data.username,
            battletag=battletag,
            avatar_url=_safe_avatar_url(data.avatar),
            game="overwatch",
            milestone_type="rank_up",
            value="\n".join(rank_up_msgs),
        ))

    # Milestone: career game count
    game_milestone = _crossed_milestone(prev_dict.get("games_played"), data.games_played, _OW_GAME_MILESTONES)
    if game_milestone:
        asyncio.create_task(_send_milestone(
            player_name=data.username,
            battletag=battletag,
            avatar_url=_safe_avatar_url(data.avatar),
            game="overwatch",
            milestone_type="games",
            value=game_milestone,
        ))


async def _snapshot_hll(steam_id: str) -> None:
    api_key = os.getenv("STEAM_API_KEY", "")
    if not api_key:
        logger.warning("STEAM_API_KEY not set — skipping HLL player %s", _slog(steam_id))
        return

    from hll_client import ProfilePrivateError as HLLPrivateError
    try:
        data = await hll_fetch_player(steam_id, api_key)
    except HLLPlayerNotFoundError:
        logger.warning("Skipping HLL player %s — Steam ID not found", _slog(steam_id))
        return
    except HLLPrivateError:
        logger.warning("Skipping HLL player %s — Steam profile is private", _slog(steam_id))
        return
    except HLLClientError as e:
        logger.error("Steam API error for %s: %s", _slog(steam_id), e)
        return
    except Exception as e:
        logger.error("Unexpected HLL client error for %s: %s", _slog(steam_id), e)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == steam_id))
        player = result.scalar_one_or_none()
        if player is None:
            return

        prev_result = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        prev_snapshot = prev_result.scalar_one_or_none()
        prev_gd_stored = (prev_snapshot.game_data or {}) if prev_snapshot else {}
        prev_playtime = prev_gd_stored.get("playtime_forever")

        player.display_name = data.display_name
        player.avatar_url = _safe_avatar_url(data.avatar)

        game_data = {
            "playtime_forever": data.playtime_forever,
            "playtime_2weeks":  data.playtime_2weeks,
            "kills":            data.kills,
            "headshots":        data.headshots,
            "tank_kills":       data.tank_kills,
            "vehicle_kills":    data.vehicle_kills,
            "artillery_kills":  data.artillery_kills,
            "sector_caps":      data.sector_caps,
            "ammo_drops":       data.ammo_drops,
            "supply_drops":     data.supply_drops,
            "commendations":    data.commendations,
            "total_xp":         data.total_xp,
            "top_role":         data.top_role,
            "role_xp":          data.role_xp,
        }

        prev_too_old_hll = prev_snapshot is None or (
            datetime.now(timezone.utc) - prev_snapshot.fetched_at.replace(tzinfo=timezone.utc)
        ).total_seconds() > 86400
        should_snapshot_hll = prev_too_old_hll or _hll_stats_changed(prev_gd_stored, game_data)

        if should_snapshot_hll:
            snapshot = StatSnapshot(
                player_id=player.id,
                fetched_at=datetime.now(timezone.utc),
                game_data=game_data,
            )
            session.add(snapshot)
            logger.info("HLL snapshot saved for %s — %s kills, %s playtime min",
                        _slog(steam_id), data.kills, data.playtime_forever)
        else:
            logger.debug("No HLL stat change for %s, skipping snapshot", _slog(steam_id))

        await session.commit()

    new_playtime = data.playtime_forever
    prev_gd = prev_gd_stored

    def _snap_stat(key):
        return prev_gd.get(key)

    if prev_playtime is not None and new_playtime is not None and new_playtime > prev_playtime:
        if steam_id not in _pending_sessions:
            _pending_sessions[steam_id] = {
                "baseline": {
                    "playtime":     prev_playtime,
                    "kills":        _snap_stat("kills"),
                    "headshots":    _snap_stat("headshots"),
                    "sector_caps":  _snap_stat("sector_caps"),
                    "total_xp":     _snap_stat("total_xp"),
                },
                "latest": {
                    "playtime":     new_playtime,
                    "kills":        data.kills,
                    "headshots":    data.headshots,
                    "sector_caps":  data.sector_caps,
                    "total_xp":     data.total_xp,
                },
                "player_name": data.display_name,
                "avatar_url":  _safe_avatar_url(data.avatar),
                "top_role":    data.top_role,
            }
        else:
            _pending_sessions[steam_id]["latest"] = {
                "playtime":    new_playtime,
                "kills":       data.kills,
                "headshots":   data.headshots,
                "sector_caps": data.sector_caps,
                "total_xp":    data.total_xp,
            }
            _pending_sessions[steam_id]["player_name"] = data.display_name
            _pending_sessions[steam_id]["avatar_url"]  = _safe_avatar_url(data.avatar)
            _pending_sessions[steam_id]["top_role"]    = data.top_role
    else:
        if steam_id in _pending_sessions:
            sess = _pending_sessions.pop(steam_id)
            b, latest = sess["baseline"], sess["latest"]

            def _delta(key):
                return (latest[key] - b[key]) if (latest.get(key) is not None and b.get(key) is not None) else None

            asyncio.create_task(_send_hll_session_report(
                player_name=sess["player_name"],
                steam_id=steam_id,
                avatar_url=sess["avatar_url"],
                duration_minutes=latest["playtime"] - b["playtime"],
                kills_delta=_delta("kills"),
                headshots_delta=_delta("headshots"),
                sector_caps_delta=_delta("sector_caps"),
                xp_delta=_delta("total_xp"),
                top_role=sess.get("top_role"),
            ))

    # Milestone: kill count
    kill_milestone = _crossed_milestone(prev_gd_stored.get("kills"), data.kills, _HLL_KILL_MILESTONES)
    if kill_milestone:
        asyncio.create_task(_send_milestone(
            player_name=data.display_name,
            battletag=steam_id,
            avatar_url=_safe_avatar_url(data.avatar),
            game="hell_let_loose",
            milestone_type="kills",
            value=kill_milestone,
        ))


async def snapshot_player(battletag: str) -> None:
    """Dispatch to the correct game's snapshot function based on the DB record."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player.game).where(Player.battletag == battletag))
        row = result.one_or_none()

    if row is None:
        return

    game = row[0]
    if game == "hell_let_loose":
        await _snapshot_hll(battletag)
    else:
        await _snapshot_ow(battletag)


def _hll_stats_changed(prev: dict, new: dict) -> bool:
    for key in ("kills", "total_xp", "playtime_forever", "headshots", "sector_caps"):
        if prev.get(key) != new.get(key):
            return True
    return False


def _ow_ranks_or_stats_changed(prev: dict, new: dict) -> bool:
    return (
        prev.get("rank_tank") != new.get("rank_tank")
        or prev.get("rank_damage") != new.get("rank_damage")
        or prev.get("rank_support") != new.get("rank_support")
        or prev.get("rank_open") != new.get("rank_open")
        or prev.get("games_played") != new.get("games_played")
        or prev.get("kda") != new.get("kda")
        or prev.get("win_rate") != new.get("win_rate")
    )


async def _send_ow_report(player_name, battletag, avatar_url, prev, new):
    try:
        from discord_bot import send_game_report
        await send_game_report(player_name, battletag, avatar_url, prev, new)
    except Exception as e:
        logger.error("Error sending OW game report for %s: %s", _slog(battletag), e)


async def _send_stats_update(player_name, battletag, avatar_url, prev, new):
    try:
        from discord_bot import send_stats_update
        await send_stats_update(player_name, battletag, avatar_url, prev, new)
    except Exception as e:
        logger.error("Error sending stats update for %s: %s", _slog(battletag), e)


async def _send_hll_session_report(player_name, steam_id, avatar_url, duration_minutes,
                                   kills_delta=None, headshots_delta=None,
                                   sector_caps_delta=None, xp_delta=None, top_role=None):
    try:
        from discord_bot import send_hll_session_report
        await send_hll_session_report(
            player_name, steam_id, avatar_url, duration_minutes,
            kills_delta, headshots_delta, sector_caps_delta, xp_delta, top_role,
        )
    except Exception as e:
        logger.error("Error sending HLL session report for %s: %s", _slog(steam_id), e)


async def _send_milestone(player_name, battletag, avatar_url, game, milestone_type, value):
    try:
        from discord_bot import send_milestone_alert
        await send_milestone_alert(player_name, battletag, avatar_url, game, milestone_type, value)
    except Exception as e:
        logger.error("Error sending milestone alert for %s: %s", _slog(battletag), e)


async def weekly_digest() -> None:
    """Compute weekly stats for all players and post digest embeds to Discord."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    week_label = cutoff.strftime("%b %d")
    ow_rows: list[dict] = []
    hll_rows: list[dict] = []

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).order_by(Player.added_at))
        players = result.scalars().all()

        for player in players:
            snaps_result = await session.execute(
                select(StatSnapshot)
                .where(StatSnapshot.player_id == player.id)
                .where(StatSnapshot.fetched_at >= cutoff)
                .order_by(StatSnapshot.fetched_at)
            )
            snaps = snaps_result.scalars().all()
            name = player.display_name or player.battletag.split("#")[0]

            if player.game == "overwatch":
                if len(snaps) < 2:
                    continue
                first, last = snaps[0], snaps[-1]
                games_delta = (last.games_played or 0) - (first.games_played or 0)
                if games_delta <= 0:
                    continue
                wins_delta = max(0, (last.games_won or 0) - (first.games_won or 0))
                wr = round(wins_delta / games_delta * 100, 1)
                stat_line = f"{games_delta} games · {wr}% WR"
                if last.kda is not None:
                    stat_line += f" · {last.kda:.2f} KDA"
                ow_rows.append({"name": name, "stat_line": stat_line, "_sort": games_delta})

            else:
                if len(snaps) < 2:
                    continue
                first_gd = snaps[0].game_data or {}
                last_gd = snaps[-1].game_data or {}
                pt_delta = (last_gd.get("playtime_forever") or 0) - (first_gd.get("playtime_forever") or 0)
                kills_delta = (last_gd.get("kills") or 0) - (first_gd.get("kills") or 0)
                if pt_delta <= 0:
                    continue
                h, m = divmod(pt_delta, 60)
                time_str = f"{h}h {m}m" if h else f"{m}m"
                stat_line = f"{time_str} played · {kills_delta:,} kills"
                hll_rows.append({"name": name, "stat_line": stat_line, "_sort": pt_delta})

    ow_rows.sort(key=lambda r: r["_sort"], reverse=True)
    hll_rows.sort(key=lambda r: r["_sort"], reverse=True)

    try:
        from discord_bot import send_weekly_digest
        await send_weekly_digest(ow_rows, hll_rows, week_label)
    except Exception as e:
        logger.error("Error sending weekly digest: %s", e)


async def poll_all_players() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player.battletag))
        battletags = result.scalars().all()

    for battletag in battletags:
        await snapshot_player(battletag)
        await asyncio.sleep(1)


def start_scheduler() -> None:
    interval = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))
    scheduler.add_job(poll_all_players, "interval", minutes=interval, id="poll_players")
    scheduler.add_job(weekly_digest, "cron", day_of_week="mon", hour=9, minute=0, id="weekly_digest")
    scheduler.start()
    logger.info("Scheduler started — polling every %d minutes", interval)


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)

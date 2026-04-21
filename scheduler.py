import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Player, StatSnapshot
from ow_client import fetch_player, ProfilePrivateError, PlayerNotFoundError, OverFastError

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def snapshot_player(battletag: str) -> None:
    try:
        data = await fetch_player(battletag)
    except ProfilePrivateError:
        logger.warning("Skipping %s — profile is private", battletag)
        return
    except PlayerNotFoundError:
        logger.warning("Skipping %s — player not found", battletag)
        return
    except OverFastError as e:
        logger.error("OverFast API error for %s: %s", battletag, e)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == battletag))
        player = result.scalar_one_or_none()
        if player is None:
            return

        # Capture previous snapshot before writing the new one
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
        }

        player.display_name = data.username
        player.avatar_url = data.avatar

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
                 "win_rate": h.win_rate, "kda": h.kda}
                for h in data.top_heroes
            ],
            stats_by_gamemode=data.stats_by_gamemode,
            raw_summary=data.raw_summary,
            raw_stats=data.raw_stats,
        )
        session.add(snapshot)
        await session.commit()
        logger.info("Snapshot saved for %s", battletag)

    # Fire game report if games were played since last snapshot
    prev_games = prev_dict["games_played"]
    new_games = data.games_played
    if (
        prev_games is not None
        and new_games is not None
        and new_games > prev_games
    ):
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
            "fetched_at": snapshot.fetched_at,
        }
        asyncio.create_task(_send_report(
            player_name=data.username,
            battletag=battletag,
            avatar_url=data.avatar,
            prev=prev_dict,
            new=new_dict,
        ))


async def _send_report(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> None:
    try:
        from discord_bot import send_game_report
        await send_game_report(player_name, battletag, avatar_url, prev, new)
    except Exception as e:
        logger.error("Error sending game report for %s: %s", battletag, e)


async def poll_all_players() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player.battletag))
        battletags = result.scalars().all()

    for battletag in battletags:
        await snapshot_player(battletag)
        await asyncio.sleep(1)  # 1s delay between requests to respect rate limits


def start_scheduler() -> None:
    interval = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))
    scheduler.add_job(poll_all_players, "interval", minutes=interval, id="poll_players")
    scheduler.start()
    logger.info("Scheduler started — polling every %d minutes", interval)


def stop_scheduler() -> None:
    scheduler.shutdown(wait=False)

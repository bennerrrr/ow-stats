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

        # Update display name and avatar from latest fetch
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
            raw_summary=data.raw_summary,
            raw_stats=data.raw_stats,
        )
        session.add(snapshot)
        await session.commit()
        logger.info("Snapshot saved for %s", battletag)


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

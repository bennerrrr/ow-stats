import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from sqlalchemy import select

load_dotenv()

from database import init_db, AsyncSessionLocal
from discord_bot import start_bot, stop_bot
from models import Player
from routers.players import router
from scheduler import start_scheduler, stop_scheduler, poll_all_players

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


async def seed_players() -> None:
    """
    Seed players from TRACKED_PLAYERS env var.
    Format: comma-separated identifiers, optionally suffixed with :hll for HLL players.
    Example: "Ben#1234,76561198012345678:hll,Alice#5678"
    """
    raw = os.getenv("TRACKED_PLAYERS", "")
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    if not entries:
        return

    async with AsyncSessionLocal() as session:
        for entry in entries:
            if entry.endswith(":hll"):
                player_id = entry[:-4]
                game = "hell_let_loose"
            else:
                player_id = entry
                game = "overwatch"

            existing = await session.execute(select(Player).where(Player.battletag == player_id))
            if existing.scalar_one_or_none() is None:
                session.add(Player(battletag=player_id, game=game))
                logger.info("Seeded player: %s (%s)", player_id, game)
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_players()
    start_scheduler()
    asyncio.create_task(poll_all_players())
    bot_task = await start_bot()
    yield
    stop_scheduler()
    await stop_bot()
    if bot_task:
        bot_task.cancel()


app = FastAPI(title="OW Stats", lifespan=lifespan)
app.include_router(router)

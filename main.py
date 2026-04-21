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
    raw = os.getenv("TRACKED_PLAYERS", "")
    battletags = [b.strip() for b in raw.split(",") if b.strip()]
    if not battletags:
        return

    async with AsyncSessionLocal() as session:
        for battletag in battletags:
            existing = await session.execute(select(Player).where(Player.battletag == battletag))
            if existing.scalar_one_or_none() is None:
                session.add(Player(battletag=battletag))
                logger.info("Seeded player: %s", battletag)
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

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

load_dotenv()

from database import init_db, AsyncSessionLocal  # noqa: E402
from discord_bot import start_bot, stop_bot  # noqa: E402
from models import Player  # noqa: E402
from routers.players import router  # noqa: E402
from routers.utils import router as utils_router  # noqa: E402
from scheduler import start_scheduler, stop_scheduler, poll_all_players  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response


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
app.add_middleware(_SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router)
app.include_router(utils_router)

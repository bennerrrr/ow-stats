import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/ow_stats.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    os.makedirs("data", exist_ok=True)
    async with engine.begin() as conn:
        from models import Player, StatSnapshot, DiscordChannel  # noqa: F401
        await conn.run_sync(Base.metadata.create_all)
        # Forward migration: add stats_by_gamemode column if not present
        try:
            await conn.execute(text(
                "ALTER TABLE stat_snapshots ADD COLUMN stats_by_gamemode JSON"
            ))
        except Exception:
            pass  # Column already exists

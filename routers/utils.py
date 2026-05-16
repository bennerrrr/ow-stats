import aiosqlite
import asyncio
import io
import os
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from database import DATABASE_URL, AsyncSessionLocal, engine
from discord_bot import bot
from scheduler import poll_all_players, scheduler
from _templates import templates

router = APIRouter(prefix="/utils", tags=["utils"])

_UTILS_TOKEN = os.getenv("UTILS_TOKEN", "")
_DB_PATH = Path(DATABASE_URL.split("///")[-1])
_start_time = time.time()


@router.get("", include_in_schema=False)
async def utils_page(request: Request):
    return templates.TemplateResponse("utils.html", {"request": request})


def _require_token(
    token: str | None = Query(None),
    authorization: str | None = Header(None),
) -> None:
    if not _UTILS_TOKEN:
        raise HTTPException(503, detail="UTILS_TOKEN not configured")
    provided = token
    if not provided and authorization:
        provided = authorization.removeprefix("Bearer ")
    if provided != _UTILS_TOKEN:
        raise HTTPException(403, detail="Invalid token")


@router.get("/health")
async def health(_: None = Depends(_require_token)) -> JSONResponse:
    db_ok = False
    player_count = 0
    snapshot_count = 0
    try:
        async with AsyncSessionLocal() as session:
            player_count = (await session.execute(text("SELECT COUNT(*) FROM players"))).scalar_one()
            snapshot_count = (await session.execute(text("SELECT COUNT(*) FROM stat_snapshots"))).scalar_one()
            db_ok = True
    except Exception:
        pass

    return JSONResponse({
        "status": "ok" if db_ok else "degraded",
        "uptime_seconds": round(time.time() - _start_time),
        "db": {
            "reachable": db_ok,
            "players": player_count,
            "snapshots": snapshot_count,
        },
        "scheduler": {
            "running": scheduler.running,
            "jobs": len(scheduler.get_jobs()),
        },
        "discord_bot": {
            "ready": bot.is_ready(),
            "latency_ms": round(bot.latency * 1000) if bot.is_ready() else None,
        },
    })


@router.get("/db/export")
async def export_db(_: None = Depends(_require_token)) -> StreamingResponse:
    db_path = str(_DB_PATH)

    def _make_snapshot() -> bytes:
        src = sqlite3.connect(db_path)
        mem = sqlite3.connect(":memory:")
        src.backup(mem)
        src.close()
        return mem.serialize()

    data = await asyncio.to_thread(_make_snapshot)
    filename = _DB_PATH.name
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/db/import")
async def import_db(file: UploadFile, _: None = Depends(_require_token)) -> JSONResponse:
    tmp_path = _DB_PATH.parent / "ow_stats.db.tmp"
    try:
        contents = await file.read()
        tmp_path.write_bytes(contents)

        # Validate it's a SQLite DB with the expected tables
        try:
            con = sqlite3.connect(str(tmp_path))
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            con.close()
            required = {"players", "stat_snapshots", "discord_channels"}
            missing = required - tables
            if missing:
                raise HTTPException(400, detail=f"Uploaded DB is missing tables: {missing}")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, detail=f"Not a valid SQLite database: {exc}") from exc

        scheduler.pause()
        try:
            await engine.dispose()
            os.rename(str(tmp_path), str(_DB_PATH))
        finally:
            scheduler.resume()

        return JSONResponse({"status": "imported", "path": str(_DB_PATH)})
    except HTTPException:
        raise
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@router.post("/db/vacuum")
async def vacuum_db(_: None = Depends(_require_token)) -> JSONResponse:
    size_before = _DB_PATH.stat().st_size
    await engine.dispose()
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        await db.execute("VACUUM")
    size_after = _DB_PATH.stat().st_size
    return JSONResponse({
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "saved_bytes": size_before - size_after,
    })


@router.post("/poll")
async def force_poll(_: None = Depends(_require_token)) -> JSONResponse:
    await poll_all_players()
    return JSONResponse({"status": "ok", "message": "Poll complete"})

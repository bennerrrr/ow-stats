import aiosqlite
import asyncio
import hmac
import io
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import DATABASE_URL, AsyncSessionLocal, engine, get_db
from discord_bot import bot, _get_setting, _set_setting, send_preview_dm
from models import DiscordChannel, Player
from scheduler import poll_all_players, weekly_digest, scheduler
from _templates import templates

router = APIRouter(prefix="/utils", tags=["utils"])

_UTILS_TOKEN = os.getenv("UTILS_TOKEN", "")
_DB_PATH = Path(DATABASE_URL.split("///")[-1])
_start_time = time.time()

_version_cache: dict = {"data": None, "fetched_at": 0.0}
_VERSION_TTL = 3600
_MAX_IMPORT_BYTES = 50 * 1024 * 1024  # 50 MB


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
    if not provided or not hmac.compare_digest(provided, _UTILS_TOKEN):
        raise HTTPException(403, detail="Invalid token")


def _sv(tag: str) -> tuple:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


async def get_version_info() -> dict | None:
    """Return cached version dict or fetch from GitHub. Returns None on failure."""
    now = time.time()
    if _version_cache["data"] and now - _version_cache["fetched_at"] < _VERSION_TTL:
        return {**_version_cache["data"], "cached": True}

    current = templates.env.globals["app_version"]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.github.com/repos/bennerrrr/ow-stats/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            latest = r.json()["tag_name"]
    except Exception:
        return None

    outdated = _sv(latest) > _sv(current)
    data = {"current": current, "latest": latest, "outdated": outdated}
    _version_cache["data"] = data
    _version_cache["fetched_at"] = now
    return {**data, "cached": False}


@router.get("/version")
async def check_version(_: None = Depends(_require_token)) -> JSONResponse:
    info = await get_version_info()
    if info is None:
        return JSONResponse({"error": "Failed to reach GitHub API"}, status_code=502)
    return JSONResponse(info)


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
        contents = await file.read(_MAX_IMPORT_BYTES + 1)
        if len(contents) > _MAX_IMPORT_BYTES:
            raise HTTPException(413, detail="File too large (max 50 MB)")
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

        from database import init_db
        await init_db()

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


@router.post("/digest")
async def force_digest(_: None = Depends(_require_token)) -> JSONResponse:
    await weekly_digest()
    return JSONResponse({"status": "ok", "message": "Digest sent"})


@router.get("/discord/channels")
async def discord_channels(
    _: None = Depends(_require_token),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(
        select(DiscordChannel).order_by(DiscordChannel.guild_id, DiscordChannel.added_at)
    )
    channels = result.scalars().all()
    guilds: dict[str, list] = {}
    for ch in channels:
        guilds.setdefault(ch.guild_id, []).append({
            "channel_id": ch.channel_id,
            "channel_name": ch.channel_name,
            "game": ch.game,
            "muted": ch.muted,
            "added_at": ch.added_at.isoformat(),
        })
    return JSONResponse({"guilds": [{"guild_id": g, "channels": chs} for g, chs in guilds.items()]})


@router.patch("/discord/channels/{channel_id}")
async def update_discord_channel(
    channel_id: str,
    game: str | None = Query(None),
    _: None = Depends(_require_token),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(select(DiscordChannel).where(DiscordChannel.channel_id == channel_id))
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(404, "Channel not registered")
    ch.game = game if game in ("overwatch", "hell_let_loose") else None
    await db.commit()
    return JSONResponse({"ok": True, "game": ch.game})


@router.delete("/discord/channels/{channel_id}")
async def remove_discord_channel(
    channel_id: str,
    _: None = Depends(_require_token),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(select(DiscordChannel).where(DiscordChannel.channel_id == channel_id))
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(404, "Channel not registered")
    await db.delete(ch)
    await db.commit()
    return JSONResponse({"ok": True})


@router.post("/discord/channels/{channel_id}/preview")
async def preview_discord_channel(
    channel_id: str,
    _: None = Depends(_require_token),
) -> JSONResponse:
    from discord_bot import send_preview_to_channel
    ok = await send_preview_to_channel(channel_id)
    if not ok:
        raise HTTPException(503, "Bot not ready or channel not found")
    return JSONResponse({"ok": True})


@router.patch("/discord/channels/{channel_id}/mute")
async def mute_discord_channel(
    channel_id: str,
    muted: bool = Query(...),
    _: None = Depends(_require_token),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(select(DiscordChannel).where(DiscordChannel.channel_id == channel_id))
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(404, "Channel not registered")
    ch.muted = muted
    await db.commit()
    return JSONResponse({"ok": True, "muted": muted})


@router.get("/discord/invite")
async def discord_invite(_: None = Depends(_require_token)) -> JSONResponse:
    if not bot.is_ready():
        raise HTTPException(503, "Bot not running")
    perms = 1024 | 2048 | 16384  # View Channel + Send Messages + Embed Links
    url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={bot.user.id}&permissions={perms}&scope=bot+applications.commands"
    )
    return JSONResponse({"url": url, "client_id": str(bot.user.id)})


@router.get("/discord/mute")
async def get_mute(_: None = Depends(_require_token)) -> JSONResponse:
    muted = await _get_setting("discord_muted") == "true"
    return JSONResponse({"muted": muted})


@router.post("/discord/mute")
async def set_mute(
    muted: bool = Query(...),
    _: None = Depends(_require_token),
) -> JSONResponse:
    await _set_setting("discord_muted", "true" if muted else "false")
    return JSONResponse({"muted": muted})


@router.get("/discord/dm")
async def get_dm_user(_: None = Depends(_require_token)) -> JSONResponse:
    user_id = await _get_setting("discord_dm_user_id")
    return JSONResponse({"user_id": user_id or ""})


@router.post("/discord/dm")
async def set_dm_user(
    user_id: str = Query(default=""),
    _: None = Depends(_require_token),
) -> JSONResponse:
    user_id = user_id.strip()
    if user_id and not re.match(r'^\d{17,20}$', user_id):
        raise HTTPException(400, detail="Invalid Discord user ID")
    await _set_setting("discord_dm_user_id", user_id or None)
    return JSONResponse({"user_id": user_id})


@router.delete("/discord/dm")
async def clear_dm_user(_: None = Depends(_require_token)) -> JSONResponse:
    await _set_setting("discord_dm_user_id", None)
    return JSONResponse({"ok": True})


@router.post("/discord/dm/test")
async def test_dm(_: None = Depends(_require_token)) -> JSONResponse:
    error = await send_preview_dm()
    if error:
        raise HTTPException(503, detail=error)
    return JSONResponse({"ok": True})


@router.get("/players")
async def list_players(
    _: None = Depends(_require_token),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(select(Player).order_by(Player.game, Player.added_at))
    players = result.scalars().all()
    return JSONResponse([
        {
            "battletag": p.battletag,
            "game": p.game,
            "display_name": p.display_name,
            "avatar_url": p.avatar_url,
        }
        for p in players
    ])

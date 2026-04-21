import asyncio
import json
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Player, StatSnapshot
from ow_client import fetch_player, ProfilePrivateError, PlayerNotFoundError, OverFastError
from scheduler import snapshot_player

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["urltag"] = lambda t: t.replace("#", "%23")


def _snapshots_to_json(snapshots) -> str:
    """Serialize snapshots oldest-first for Chart.js consumption."""
    return json.dumps([
        {
            "date": s.fetched_at.strftime("%b %d %H:%M"),
            "win_rate": round(s.win_rate * 100, 1) if s.win_rate is not None else None,
            "kda": round(s.kda, 2) if s.kda is not None else None,
            "games_played": s.games_played,
        }
        for s in reversed(list(snapshots))
    ])


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).order_by(Player.added_at))
    players = result.scalars().all()

    # Load latest snapshot for each player
    player_data = []
    for player in players:
        snap_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        latest = snap_result.scalar_one_or_none()
        player_data.append({"player": player, "snapshot": latest})

    return templates.TemplateResponse("index.html", {"request": request, "players": player_data})


@router.get("/players/{battletag:path}", response_class=HTMLResponse)
async def player_detail(request: Request, battletag: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).where(Player.battletag == battletag))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    snaps_result = await db.execute(
        select(StatSnapshot)
        .where(StatSnapshot.player_id == player.id)
        .order_by(StatSnapshot.fetched_at.desc())
        .limit(30)
    )
    snapshots = snaps_result.scalars().all()

    return templates.TemplateResponse(
        "player.html", {
            "request": request,
            "player": player,
            "snapshots": snapshots,
            "snapshots_json": _snapshots_to_json(snapshots),
        }
    )


@router.post("/players/add")
async def add_player(
    request: Request,
    battletag: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    battletag = battletag.strip()

    # Check for duplicate
    existing = await db.execute(select(Player).where(Player.battletag == battletag))
    if existing.scalar_one_or_none():
        return RedirectResponse("/?error=already_tracked", status_code=303)

    # Validate the player exists and profile is accessible
    try:
        data = await fetch_player(battletag)
    except PlayerNotFoundError:
        return RedirectResponse("/?error=not_found", status_code=303)
    except ProfilePrivateError:
        return RedirectResponse("/?error=private", status_code=303)
    except OverFastError:
        return RedirectResponse("/?error=api_error", status_code=303)

    player = Player(
        battletag=battletag,
        display_name=data.username,
        avatar_url=data.avatar,
    )
    db.add(player)
    await db.commit()
    await db.refresh(player)

    # Take an initial snapshot immediately
    await snapshot_player(battletag)

    return RedirectResponse("/", status_code=303)


@router.post("/players/{battletag:path}/delete")
async def delete_player(battletag: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).where(Player.battletag == battletag))
    player = result.scalar_one_or_none()
    if player:
        await db.delete(player)
        await db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/players/{battletag:path}/refresh")
async def refresh_player(battletag: str, request: Request, db: AsyncSession = Depends(get_db)):
    from ow_client import invalidate_cache
    invalidate_cache(battletag)
    await snapshot_player(battletag)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        result = await db.execute(select(Player).where(Player.battletag == battletag))
        player = result.scalar_one_or_none()
        if player is None:
            raise HTTPException(status_code=404)
        snaps_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(30)
        )
        snapshots = snaps_result.scalars().all()
        return templates.TemplateResponse(
            "partials/player_live.html", {
                "request": request,
                "player": player,
                "snapshots": snapshots,
                "snapshots_json": _snapshots_to_json(snapshots),
            }
        )

    return RedirectResponse(f"/players/{battletag.replace('#', '%23')}", status_code=303)

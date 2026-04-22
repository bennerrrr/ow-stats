import asyncio
import json
from datetime import timezone
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Player, StatSnapshot
from ow_client import fetch_player, ProfilePrivateError, PlayerNotFoundError, OverFastError, HERO_ROLES
from scheduler import snapshot_player

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["urltag"] = lambda t: t.replace("#", "%23")


_ROLE_ORDER = ["tank", "damage", "support"]
_ROLE_LABELS = {"tank": "Tank", "damage": "Damage", "support": "Support"}
_ROLE_COLORS = {"tank": "blue", "damage": "red", "support": "green"}


def _snapshots_to_json(snapshots) -> str:
    """Emit one point per snapshot where tracked stats actually changed, oldest-first."""
    result = []
    prev = None
    for s in reversed(snapshots):  # snapshots arrive newest-first; iterate oldest-first
        wr = round(s.win_rate * 100, 1) if s.win_rate is not None else None
        kda = round(s.kda, 2) if s.kda is not None else None
        gp = s.games_played
        if prev is None or (wr, kda, gp) != prev:
            dt = s.fetched_at
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            result.append({
                "date": dt.strftime("%b %d %H:%M"),
                "win_rate": wr,
                "kda": kda,
                "games_played": gp,
            })
            prev = (wr, kda, gp)
    return json.dumps(result)


def _compute_sessions(snapshots) -> list[dict]:
    """Return per-session deltas between consecutive snapshots where games_played increased."""
    ordered = list(reversed(snapshots))  # oldest → newest
    sessions = []
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        if curr.games_played is None or prev.games_played is None:
            continue
        delta_games = curr.games_played - prev.games_played
        if delta_games <= 0:
            continue
        delta_wins = max(0, (curr.games_won or 0) - (prev.games_won or 0))
        delta_losses = delta_games - delta_wins
        session_wr = round(delta_wins / delta_games * 100, 1)
        kda_delta = round(curr.kda - prev.kda, 2) if (curr.kda is not None and prev.kda is not None) else None

        dt_start = prev.fetched_at
        dt_end = curr.fetched_at
        if dt_start.tzinfo is None:
            dt_start = dt_start.replace(tzinfo=timezone.utc)
        if dt_end.tzinfo is None:
            dt_end = dt_end.replace(tzinfo=timezone.utc)

        sessions.append({
            "start": dt_start.strftime("%b %d %H:%M"),
            "end": dt_end.strftime("%b %d %H:%M"),
            "games": delta_games,
            "wins": delta_wins,
            "losses": delta_losses,
            "win_rate": session_wr,
            "kda_delta": kda_delta,
        })
    return list(reversed(sessions))  # most recent first


def _compute_role_stats(top_heroes: list | None) -> list[dict]:
    """Aggregate hero stats by role, weighted by time played."""
    if not top_heroes:
        return []
    acc: dict[str, dict] = {}
    for h in top_heroes:
        role = HERO_ROLES.get((h.get("hero") or "").lower())
        if not role:
            continue
        if role not in acc:
            acc[role] = {"tp": 0,
                         "kda_w": 0.0, "kda_t": 0,
                         "wr_w": 0.0,  "wr_t": 0,
                         "dmg_w": 0.0, "dmg_t": 0,
                         "heal_w": 0.0, "heal_t": 0,
                         "elim_w": 0.0, "elim_t": 0}
        r = acc[role]
        tp = h.get("time_played") or 0
        r["tp"] += tp
        if tp > 0:
            for val_key, w_key, t_key in [
                ("kda",                    "kda_w",  "kda_t"),
                ("win_rate",               "wr_w",   "wr_t"),
                ("damage_per_10_min",      "dmg_w",  "dmg_t"),
                ("healing_per_10_min",     "heal_w", "heal_t"),
                ("eliminations_per_10_min","elim_w", "elim_t"),
            ]:
                v = h.get(val_key)
                if v is not None:
                    r[w_key] += v * tp
                    r[t_key] += tp

    result = []
    for role in _ROLE_ORDER:
        if role not in acc or acc[role]["tp"] == 0:
            continue
        r = acc[role]
        def wavg(w, t): return round(w / t, 2) if t > 0 else None
        result.append({
            "role":               _ROLE_LABELS[role],
            "role_key":           role,
            "color":              _ROLE_COLORS[role],
            "time_played":        r["tp"],
            "kda":                wavg(r["kda_w"],  r["kda_t"]),
            "win_rate":           round(r["wr_w"] / r["wr_t"] * 100, 1) if r["wr_t"] > 0 else None,
            "damage_per_10_min":  round(r["dmg_w"]  / r["dmg_t"])  if r["dmg_t"]  > 0 else None,
            "healing_per_10_min": round(r["heal_w"] / r["heal_t"]) if r["heal_t"] > 0 else None,
            "elims_per_10_min":   round(r["elim_w"] / r["elim_t"], 1) if r["elim_t"] > 0 else None,
        })
    return result


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
        .limit(100)
    )
    snapshots = snaps_result.scalars().all()

    role_stats = _compute_role_stats(snapshots[0].top_heroes if snapshots else None)
    return templates.TemplateResponse(
        "player.html", {
            "request": request,
            "player": player,
            "snapshots": snapshots,
            "snapshots_json": _snapshots_to_json(snapshots),
            "role_stats": role_stats,
            "sessions": _compute_sessions(snapshots),
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
            .limit(100)
        )
        snapshots = snaps_result.scalars().all()
        role_stats = _compute_role_stats(snapshots[0].top_heroes if snapshots else None)
        return templates.TemplateResponse(
            "partials/player_live.html", {
                "request": request,
                "player": player,
                "snapshots": snapshots,
                "snapshots_json": _snapshots_to_json(snapshots),
                "role_stats": role_stats,
                "sessions": _compute_sessions(snapshots),
            }
        )

    return RedirectResponse(f"/players/{battletag.replace('#', '%23')}", status_code=303)

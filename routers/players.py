import asyncio
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Player, StatSnapshot
from ow_client import fetch_player as ow_fetch_player, ProfilePrivateError, PlayerNotFoundError as OWPlayerNotFoundError, OverFastError, HERO_ROLES
from scheduler import snapshot_player

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.filters["urltag"] = lambda t: t.replace("#", "%23")

try:
    _DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "America/New_York"))
except ZoneInfoNotFoundError:
    _DISPLAY_TZ = ZoneInfo("UTC")


def _to_display_tz(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_TZ)


templates.env.filters["localdt"] = lambda dt, fmt="%b %d, %Y %H:%M %Z": _to_display_tz(dt).strftime(fmt)


_ROLE_ORDER = ["tank", "damage", "support"]
_ROLE_LABELS = {"tank": "Tank", "damage": "Damage", "support": "Support"}
_ROLE_COLORS = {"tank": "blue", "damage": "red", "support": "green"}


def _snapshots_to_json(snapshots) -> str:
    """OW: one chart point per snapshot where tracked stats changed, oldest-first."""
    result = []
    prev = None
    for s in reversed(snapshots):
        wr = round(s.win_rate * 100, 1) if s.win_rate is not None else None
        kda = round(s.kda, 2) if s.kda is not None else None
        gp = s.games_played
        if prev is None or (wr, kda, gp) != prev:
            result.append({
                "date": _to_display_tz(s.fetched_at).strftime("%b %d %H:%M %Z"),
                "win_rate": wr,
                "kda": kda,
                "games_played": gp,
            })
            prev = (wr, kda, gp)
    return json.dumps(result)


def _hll_snapshots_to_json(snapshots) -> str:
    """HLL: one chart point per snapshot where kills or XP changed, oldest-first."""
    result = []
    prev = (None, None)
    for s in reversed(snapshots):
        gd = s.game_data or {}
        kills = gd.get("kills")
        xp = gd.get("total_xp")
        if (kills, xp) != prev:
            result.append({
                "date": _to_display_tz(s.fetched_at).strftime("%b %d %H:%M %Z"),
                "kills": kills,
                "xp": xp,
            })
            prev = (kills, xp)
    return json.dumps(result)


def _compute_sessions(snapshots) -> list[dict]:
    """OW: per-session deltas between consecutive snapshots where games_played increased."""
    ordered = list(reversed(snapshots))
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

        sessions.append({
            "start": _to_display_tz(prev.fetched_at).strftime("%b %d %H:%M %Z"),
            "end": _to_display_tz(curr.fetched_at).strftime("%b %d %H:%M %Z"),
            "games": delta_games,
            "wins": delta_wins,
            "losses": delta_losses,
            "win_rate": session_wr,
            "kda_delta": kda_delta,
        })
    return list(reversed(sessions))


def _compute_hll_sessions(snapshots) -> list[dict]:
    """HLL: derive play sessions from playtime_forever deltas between consecutive snapshots."""
    ordered = list(reversed(snapshots))  # oldest → newest
    sessions = []
    for i in range(1, len(ordered)):
        prev, curr = ordered[i - 1], ordered[i]
        pgd, cgd = prev.game_data or {}, curr.game_data or {}
        prev_pt = pgd.get("playtime_forever")
        curr_pt = cgd.get("playtime_forever")
        if prev_pt is None or curr_pt is None:
            continue
        delta_minutes = curr_pt - prev_pt
        if delta_minutes <= 0:
            continue
        prev_kills = pgd.get("kills")
        curr_kills = cgd.get("kills")
        kills_delta = (curr_kills - prev_kills) if (curr_kills is not None and prev_kills is not None) else None
        prev_xp = pgd.get("total_xp")
        curr_xp = cgd.get("total_xp")
        xp_delta = (curr_xp - prev_xp) if (curr_xp is not None and prev_xp is not None) else None
        sessions.append({
            "start": _to_display_tz(prev.fetched_at).strftime("%b %d %H:%M %Z"),
            "duration": _fmt_duration(delta_minutes * 60),
            "duration_minutes": delta_minutes,
            "kills_delta": kills_delta,
            "xp_delta": xp_delta,
        })
    return list(reversed(sessions))  # most recent first


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _compute_role_stats(top_heroes: list | None) -> list[dict]:
    """OW: aggregate hero stats by role, weighted by time played."""
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

    ow_players, hll_players = [], []
    for player in players:
        snap_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        latest = snap_result.scalar_one_or_none()
        item = {"player": player, "snapshot": latest}
        if player.game == "hell_let_loose":
            hll_players.append(item)
        else:
            ow_players.append(item)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "ow_players": ow_players,
        "hll_players": hll_players,
    })


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

    ctx = _build_player_context(player, snapshots)
    return templates.TemplateResponse("player.html", {"request": request, **ctx})


def _build_player_context(player: Player, snapshots) -> dict:
    if player.game == "hell_let_loose":
        return {
            "player": player,
            "snapshots": snapshots,
            "snapshots_json": _hll_snapshots_to_json(snapshots),
            "sessions": _compute_hll_sessions(snapshots),
            "role_stats": [],
        }
    return {
        "player": player,
        "snapshots": snapshots,
        "snapshots_json": _snapshots_to_json(snapshots),
        "role_stats": _compute_role_stats(snapshots[0].top_heroes if snapshots else None),
        "sessions": _compute_sessions(snapshots),
    }


@router.post("/players/add")
async def add_player(
    request: Request,
    player_id: str = Form(...),
    game: str = Form(default="overwatch"),
    db: AsyncSession = Depends(get_db),
):
    player_id = player_id.strip()
    game = game.strip()

    existing = await db.execute(select(Player).where(Player.battletag == player_id))
    if existing.scalar_one_or_none():
        return RedirectResponse("/?error=already_tracked", status_code=303)

    if game == "hell_let_loose":
        return await _add_hll_player_web(player_id, db)
    else:
        return await _add_ow_player_web(player_id, db)


async def _add_ow_player_web(battletag: str, db: AsyncSession):
    try:
        data = await ow_fetch_player(battletag)
    except OWPlayerNotFoundError:
        return RedirectResponse("/?error=not_found", status_code=303)
    except ProfilePrivateError:
        return RedirectResponse("/?error=private", status_code=303)
    except OverFastError:
        return RedirectResponse("/?error=api_error", status_code=303)

    player = Player(battletag=battletag, game="overwatch", display_name=data.username, avatar_url=data.avatar)
    db.add(player)
    await db.commit()
    await db.refresh(player)
    await snapshot_player(battletag)
    return RedirectResponse("/", status_code=303)


async def _add_hll_player_web(steam_id: str, db: AsyncSession):
    from hll_client import fetch_player as hll_fetch, PlayerNotFoundError, ProfilePrivateError, HLLClientError
    api_key = os.getenv("STEAM_API_KEY", "")
    try:
        data = await hll_fetch(steam_id, api_key)
    except PlayerNotFoundError:
        return RedirectResponse("/?error=hll_not_found", status_code=303)
    except ProfilePrivateError:
        return RedirectResponse("/?error=hll_private", status_code=303)
    except (HLLClientError, Exception):
        return RedirectResponse("/?error=api_error", status_code=303)

    player = Player(battletag=steam_id, game="hell_let_loose", display_name=data.display_name, avatar_url=data.avatar)
    db.add(player)
    await db.commit()
    await db.refresh(player)
    await snapshot_player(steam_id)
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
    result = await db.execute(select(Player).where(Player.battletag == battletag))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=404)

    if player.game == "overwatch":
        from ow_client import invalidate_cache
        invalidate_cache(battletag)
    else:
        from hll_client import invalidate_cache as hll_invalidate
        hll_invalidate(battletag)

    await snapshot_player(battletag)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        snaps_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(100)
        )
        snapshots = snaps_result.scalars().all()
        ctx = _build_player_context(player, snapshots)
        return templates.TemplateResponse(
            "partials/player_live.html", {"request": request, **ctx}
        )

    return RedirectResponse(f"/players/{battletag.replace('#', '%23')}", status_code=303)

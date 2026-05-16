import json
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import Player, StatSnapshot
from ow_client import fetch_player as ow_fetch_player, ProfilePrivateError, PlayerNotFoundError as OWPlayerNotFoundError, OverFastError, InvalidBattletagError, HERO_ROLES
from routers.utils import get_version_info
from scheduler import snapshot_player
from _templates import templates

router = APIRouter()
templates.env.filters["urltag"] = lambda t: t.replace("#", "%23")

_ALLOWED_GAMES = {"overwatch", "hell_let_loose"}
_STEAM_ID_RE = re.compile(r'^\d{17}$')

try:
    _DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "America/New_York"))
except ZoneInfoNotFoundError:
    _DISPLAY_TZ = ZoneInfo("UTC")


def _to_display_tz(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_TZ)


templates.env.filters["localdt"] = lambda dt, fmt="%b %d, %Y %H:%M %Z": _to_display_tz(dt).strftime(fmt)


def _timeago(dt) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 7200:
        return "LIVE"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 604800:
        return f"{int(secs // 86400)}d ago"
    return f"{int(secs // 604800)}w ago"


templates.env.filters["timeago"] = _timeago


_ROLE_ORDER = ["tank", "damage", "support"]
_ROLE_LABELS = {"tank": "Tank", "damage": "Damage", "support": "Support"}
_ROLE_COLORS = {"tank": "blue", "damage": "red", "support": "green"}


def _safe_json(data) -> str:
    """json.dumps with HTML-unsafe chars escaped to prevent </script> injection."""
    return (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _safe_avatar_url(url: str | None) -> str | None:
    """Accept only HTTPS avatar URLs of sane length; drop anything else."""
    if url and url.startswith("https://") and len(url) <= 500:
        return url
    return None


def _snapshots_to_json(snapshots) -> str:
    """OW: one chart point per snapshot where tracked stats changed, oldest-first."""
    result = []
    prev = None
    for s in reversed(snapshots):
        wr = round(s.win_rate * 100, 1) if s.win_rate is not None else None
        kda = round(s.kda, 2) if s.kda is not None else None
        gp = s.games_played
        sbg = s.stats_by_gamemode or {}
        comp = sbg.get("competitive") or {}
        qp = sbg.get("quickplay") or {}
        comp_wr = round(comp.get("win_rate") * 100, 1) if comp.get("win_rate") is not None else None
        qp_wr = round(qp.get("win_rate") * 100, 1) if qp.get("win_rate") is not None else None
        if prev is None or (wr, kda, gp) != prev:
            ts = s.fetched_at if s.fetched_at.tzinfo else s.fetched_at.replace(tzinfo=timezone.utc)
            result.append({
                "ts": ts.isoformat(),
                "date": _to_display_tz(s.fetched_at).strftime("%b %d %H:%M %Z"),
                "win_rate": wr,
                "kda": kda,
                "games_played": gp,
                "comp_win_rate": comp_wr,
                "qp_win_rate": qp_wr,
            })
            prev = (wr, kda, gp)
    return _safe_json(result)


def _hll_snapshots_to_json(snapshots) -> str:
    """HLL: one chart point per snapshot where kills or XP changed, oldest-first."""
    result = []
    prev = (None, None)
    for s in reversed(snapshots):
        gd = s.game_data or {}
        kills = gd.get("kills")
        xp = gd.get("total_xp")
        if (kills, xp) != prev:
            ts = s.fetched_at if s.fetched_at.tzinfo else s.fetched_at.replace(tzinfo=timezone.utc)
            result.append({
                "ts": ts.isoformat(),
                "date": _to_display_tz(s.fetched_at).strftime("%b %d %H:%M %Z"),
                "kills": kills,
                "xp": xp,
                "headshots": gd.get("headshots"),
                "sector_caps": gd.get("sector_caps"),
            })
            prev = (kills, xp)
    return _safe_json(result)


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

        ts = prev.fetched_at if prev.fetched_at.tzinfo else prev.fetched_at.replace(tzinfo=timezone.utc)
        sessions.append({
            "start": _to_display_tz(prev.fetched_at).strftime("%b %d %H:%M %Z"),
            "end": _to_display_tz(curr.fetched_at).strftime("%b %d %H:%M %Z"),
            "ts_iso": ts.isoformat(),
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
        ts = prev.fetched_at if prev.fetched_at.tzinfo else prev.fetched_at.replace(tzinfo=timezone.utc)
        sessions.append({
            "start": _to_display_tz(prev.fetched_at).strftime("%b %d %H:%M %Z"),
            "ts_iso": ts.isoformat(),
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


def _tz(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _trend_baseline(snaps, days: int = 7):
    """Return (baseline_snap, period_days) from a desc-ordered snap list."""
    if len(snaps) < 2:
        return None, 0
    latest_time = _tz(snaps[0].fetched_at)
    cutoff = latest_time - timedelta(days=days)
    baseline = None
    for s in snaps[1:]:
        if _tz(s.fetched_at) <= cutoff:
            baseline = s
            break
    if baseline is None:
        baseline = snaps[-1]
    period = round((latest_time - _tz(baseline.fetched_at)).total_seconds() / 86400)
    return baseline, period


def _compute_rank_history(snapshots) -> list[dict]:
    """Return one entry per distinct rank state, newest first."""
    if not snapshots:
        return []
    history = []
    prev = None
    for snap in reversed(snapshots):  # oldest → newest
        cur = (snap.rank_tank, snap.rank_damage, snap.rank_support, snap.rank_open)
        if cur != prev and any(cur):
            history.append({
                "fetched_at": snap.fetched_at,
                "rank_tank":    snap.rank_tank,
                "rank_damage":  snap.rank_damage,
                "rank_support": snap.rank_support,
                "rank_open":    snap.rank_open,
            })
            prev = cur
    return list(reversed(history))  # newest first


def _rank_history_to_json(rank_history: list[dict]) -> str:
    result = []
    for entry in reversed(rank_history):  # oldest first for charting
        ts = entry["fetched_at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        result.append({
            "ts":      ts.isoformat(),
            "date":    _to_display_tz(ts).strftime("%b %d"),
            "tank":    entry["rank_tank"],
            "damage":  entry["rank_damage"],
            "support": entry["rank_support"],
            "open":    entry["rank_open"],
        })
    return _safe_json(result)


def _compute_ow_trend(snaps) -> dict | None:
    baseline, period = _trend_baseline(snaps)
    if baseline is None:
        return None
    latest = snaps[0]
    games_delta = wr_delta = kda_delta = None
    if latest.games_played is not None and baseline.games_played is not None:
        games_delta = latest.games_played - baseline.games_played
    if latest.win_rate is not None and baseline.win_rate is not None:
        wr_delta = round((latest.win_rate - baseline.win_rate) * 100, 1)
    if latest.kda is not None and baseline.kda is not None:
        kda_delta = round(latest.kda - baseline.kda, 2)
    return {"games_delta": games_delta, "wr_delta": wr_delta, "kda_delta": kda_delta, "period": period}


def _compute_hll_trend(snaps) -> dict | None:
    baseline, period = _trend_baseline(snaps)
    if baseline is None:
        return None
    latest = snaps[0]
    lgd, bgd = latest.game_data or {}, baseline.game_data or {}
    kills_delta = pt_delta = None
    if lgd.get("kills") is not None and bgd.get("kills") is not None:
        kills_delta = lgd["kills"] - bgd["kills"]
    if lgd.get("playtime_forever") is not None and bgd.get("playtime_forever") is not None:
        pt_delta = lgd["playtime_forever"] - bgd["playtime_forever"]
    return {"kills_delta": kills_delta, "pt_delta": pt_delta, "period": period}


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).order_by(Player.added_at))
    players = result.scalars().all()

    ow_rows = []
    hll_rows = []
    for player in players:
        snap_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        snap = snap_result.scalar_one_or_none()
        name = player.display_name or player.battletag.split("#")[0]
        if player.game == "hell_let_loose":
            gd = (snap.game_data or {}) if snap else {}
            kills = gd.get("kills")
            pt = gd.get("playtime_forever")
            hs = gd.get("headshots")
            kph = round(kills / (pt / 60), 1) if (kills and pt and pt > 0) else None
            hs_pct = round(hs / kills * 100, 1) if (hs is not None and kills) else None
            hll_rows.append({
                "battletag": player.battletag,
                "name": name,
                "avatar_url": player.avatar_url,
                "kills": kills,
                "kph": kph,
                "pt_hours": round(pt / 60, 1) if pt else None,
                "hs_pct": hs_pct,
                "fetched_at": snap.fetched_at if snap else None,
            })
        else:
            wr = round(snap.win_rate * 100, 1) if (snap and snap.win_rate is not None) else None
            kda = snap.kda if snap else None
            top_hero = (snap.top_heroes or [None])[0] if snap else None
            ow_rows.append({
                "battletag": player.battletag,
                "name": name,
                "avatar_url": player.avatar_url,
                "win_rate": wr,
                "kda": kda,
                "games": snap.games_played if snap else None,
                "top_hero": top_hero.get("name") if top_hero else None,
                "fetched_at": snap.fetched_at if snap else None,
            })

    return templates.TemplateResponse("leaderboard.html", {
        "request": request,
        "ow_rows": ow_rows,
        "hll_rows": hll_rows,
    })


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).order_by(Player.added_at))
    players = result.scalars().all()
    ow_count = sum(1 for p in players if p.game != "hell_let_loose")
    hll_count = sum(1 for p in players if p.game == "hell_let_loose")
    version_info = await get_version_info()
    update_available = bool(version_info and version_info.get("outdated"))
    latest_version = version_info.get("latest", "") if version_info else ""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "ow_count": ow_count,
        "hll_count": hll_count,
        "update_available": update_available,
        "latest_version": latest_version,
    })


async def _build_game_page(request: Request, game: str, db: AsyncSession):
    result = await db.execute(select(Player).order_by(Player.added_at))
    players = result.scalars().all()
    items = []
    index_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for player in players:
        if player.game != game:
            continue
        snap_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .where(StatSnapshot.fetched_at >= index_cutoff)
            .order_by(StatSnapshot.fetched_at.desc())
        )
        snaps = snap_result.scalars().all()
        latest = snaps[0] if snaps else None
        if game == "hell_let_loose":
            items.append({"player": player, "snapshot": latest, "trend": _compute_hll_trend(snaps)})
        else:
            items.append({"player": player, "snapshot": latest, "trend": _compute_ow_trend(snaps)})
    return items


@router.get("/overwatch", response_class=HTMLResponse)
async def overwatch_page(request: Request, db: AsyncSession = Depends(get_db)):
    players = await _build_game_page(request, "overwatch", db)
    return templates.TemplateResponse("overwatch.html", {"request": request, "players": players})


@router.get("/hll", response_class=HTMLResponse)
async def hll_page(request: Request, db: AsyncSession = Depends(get_db)):
    players = await _build_game_page(request, "hell_let_loose", db)
    return templates.TemplateResponse("hll.html", {"request": request, "players": players})


@router.get("/players/{battletag:path}", response_class=HTMLResponse)
async def player_detail(request: Request, battletag: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).where(Player.battletag == battletag))
    player = result.scalar_one_or_none()
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    snaps_result = await db.execute(
        select(StatSnapshot)
        .where(StatSnapshot.player_id == player.id)
        .where(StatSnapshot.fetched_at >= cutoff)
        .order_by(StatSnapshot.fetched_at.desc())
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
            "trend": _compute_hll_trend(snapshots),
        }
    return {
        "player": player,
        "snapshots": snapshots,
        "snapshots_json": _snapshots_to_json(snapshots),
        "role_stats": _compute_role_stats(snapshots[0].top_heroes if snapshots else None),
        "sessions": _compute_sessions(snapshots),
        "trend": _compute_ow_trend(snapshots),
        "rank_history": (rh := _compute_rank_history(snapshots)),
        "rank_history_json": _rank_history_to_json(rh),
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
    if game not in _ALLOWED_GAMES:
        game = "overwatch"

    if not player_id or len(player_id) > 100:
        redirect_base = "/hll" if game == "hell_let_loose" else "/overwatch"
        return RedirectResponse(f"{redirect_base}?error=invalid_id", status_code=303)

    existing = await db.execute(select(Player).where(Player.battletag == player_id))
    redirect_base = "/hll" if game == "hell_let_loose" else "/overwatch"
    if existing.scalar_one_or_none():
        return RedirectResponse(f"{redirect_base}?error=already_tracked", status_code=303)

    if game == "hell_let_loose":
        if not _STEAM_ID_RE.match(player_id):
            return RedirectResponse("/hll?error=invalid_steam_id", status_code=303)
        return await _add_hll_player_web(player_id, db)
    else:
        return await _add_ow_player_web(player_id, db)


async def _add_ow_player_web(battletag: str, db: AsyncSession):
    try:
        data = await ow_fetch_player(battletag)
    except InvalidBattletagError:
        return RedirectResponse("/overwatch?error=invalid_battletag", status_code=303)
    except OWPlayerNotFoundError:
        return RedirectResponse("/overwatch?error=not_found", status_code=303)
    except ProfilePrivateError:
        return RedirectResponse("/overwatch?error=private", status_code=303)
    except OverFastError:
        return RedirectResponse("/overwatch?error=api_error", status_code=303)

    player = Player(battletag=battletag, game="overwatch", display_name=data.username, avatar_url=_safe_avatar_url(data.avatar))
    db.add(player)
    await db.commit()
    await db.refresh(player)
    await snapshot_player(battletag)
    name = quote(data.username or battletag, safe="")
    return RedirectResponse(f"/overwatch?added={name}", status_code=303)


async def _add_hll_player_web(steam_id: str, db: AsyncSession):
    from hll_client import fetch_player as hll_fetch, PlayerNotFoundError, ProfilePrivateError, HLLClientError
    api_key = os.getenv("STEAM_API_KEY", "")
    try:
        data = await hll_fetch(steam_id, api_key)
    except PlayerNotFoundError:
        return RedirectResponse("/hll?error=hll_not_found", status_code=303)
    except ProfilePrivateError:
        return RedirectResponse("/hll?error=hll_private", status_code=303)
    except (HLLClientError, Exception):
        return RedirectResponse("/hll?error=api_error", status_code=303)

    player = Player(battletag=steam_id, game="hell_let_loose", display_name=data.display_name, avatar_url=_safe_avatar_url(data.avatar))
    db.add(player)
    await db.commit()
    await db.refresh(player)
    await snapshot_player(steam_id)
    name = quote(data.display_name or steam_id, safe="")
    return RedirectResponse(f"/hll?added={name}", status_code=303)


@router.post("/players/{battletag:path}/delete")
async def delete_player(battletag: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Player).where(Player.battletag == battletag))
    player = result.scalar_one_or_none()
    if player:
        game = player.game
        await db.delete(player)
        await db.commit()
        return RedirectResponse("/hll" if game == "hell_let_loose" else "/overwatch", status_code=303)
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
        await db.refresh(player)
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        snaps_result = await db.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .where(StatSnapshot.fetched_at >= cutoff)
            .order_by(StatSnapshot.fetched_at.desc())
        )
        snapshots = snaps_result.scalars().all()
        if not snapshots:
            raise HTTPException(status_code=503, detail="No snapshots available")
        ctx = _build_player_context(player, snapshots)
        return templates.TemplateResponse(
            "partials/player_live.html", {"request": request, **ctx}
        )

    return RedirectResponse(f"/players/{quote(player.battletag, safe='')}", status_code=303)

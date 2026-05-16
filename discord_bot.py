import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select, or_

from database import AsyncSessionLocal
from models import DiscordChannel, Player, StatSnapshot
from ow_client import OverFastError, PlayerNotFoundError as OWPlayerNotFoundError, ProfilePrivateError, fetch_player as ow_fetch_player

logger = logging.getLogger(__name__)


def _slog(value: str) -> str:
    return str(value).replace("\n", " ").replace("\r", " ")

OW_COLOR  = 0xF99E1A
HLL_COLOR = 0x5C6BC0  # muted indigo — military feel
WIN_COLOR  = 0x57F287
LOSS_COLOR = 0xED4245
TIE_COLOR  = 0xFEE75C

RANK_EMOJIS = {
    "bronze": "🟤",
    "silver": "⚪",
    "gold": "🟡",
    "platinum": "🔵",
    "diamond": "💎",
    "master": "🔴",
    "grandmaster": "🟠",
    "champion": "👑",
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

_notification_queue: list[Callable[[], Coroutine]] = []


async def _flush_notification_queue() -> None:
    if not _notification_queue:
        return
    queued = _notification_queue.copy()
    _notification_queue.clear()
    logger.info("Flushing %d queued notification(s) after reconnect", len(queued))
    for factory in queued:
        await factory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rank_display(rank: str | None) -> str:
    if not rank:
        return "—"
    tier = rank.split()[0].lower()
    emoji = RANK_EMOJIS.get(tier, "")
    return f"{emoji} {rank}" if emoji else rank


def _fmt_time(seconds: int) -> str:
    h, m = divmod(seconds, 3600)
    m //= 60
    return f"{h}h {m}m" if h else f"{m}m"


def _snapshot_to_dict(snapshot: StatSnapshot) -> dict:
    """Capture snapshot values while the session is open."""
    return {
        "games_played": snapshot.games_played,
        "games_won": snapshot.games_won,
        "games_lost": snapshot.games_lost,
        "kda": snapshot.kda,
        "win_rate": snapshot.win_rate,
        "rank_tank": snapshot.rank_tank,
        "rank_damage": snapshot.rank_damage,
        "rank_support": snapshot.rank_support,
        "rank_open": snapshot.rank_open,
        "top_heroes": snapshot.top_heroes,
        "fetched_at": snapshot.fetched_at,
    }


def _discord_timeago(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 7200:
        return "just now"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 604800:
        return f"{int(secs // 86400)}d ago"
    return f"{int(secs // 604800)}w ago"


def _trend_baseline(snaps, days: int = 7):
    """Return (baseline_snap, period_days) from a desc-ordered snapshot list."""
    if len(snaps) < 2:
        return None, 0
    latest_time = snaps[0].fetched_at if snaps[0].fetched_at.tzinfo else snaps[0].fetched_at.replace(tzinfo=timezone.utc)
    cutoff = latest_time - timedelta(days=days)
    baseline = None
    for s in snaps[1:]:
        st = s.fetched_at if s.fetched_at.tzinfo else s.fetched_at.replace(tzinfo=timezone.utc)
        if st <= cutoff:
            baseline = s
            break
    if baseline is None:
        baseline = snaps[-1]
    bt = baseline.fetched_at if baseline.fetched_at.tzinfo else baseline.fetched_at.replace(tzinfo=timezone.utc)
    period = round((latest_time - bt).total_seconds() / 86400)
    return baseline, period


def _ow_trend(snaps) -> dict | None:
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


def _hll_trend(snaps) -> dict | None:
    baseline, period = _trend_baseline(snaps)
    if baseline is None:
        return None
    lgd = snaps[0].game_data or {}
    bgd = baseline.game_data or {}
    kills_delta = pt_delta = None
    if lgd.get("kills") is not None and bgd.get("kills") is not None:
        kills_delta = lgd["kills"] - bgd["kills"]
    if lgd.get("playtime_forever") is not None and bgd.get("playtime_forever") is not None:
        pt_delta = lgd["playtime_forever"] - bgd["playtime_forever"]
    return {"kills_delta": kills_delta, "pt_delta": pt_delta, "period": period}


def _all_ow_sessions(snaps, limit: int = 10) -> list[dict]:
    ordered = list(reversed(snaps))  # oldest → newest
    sessions = []
    for i in range(len(ordered) - 1, 0, -1):
        prev, curr = ordered[i - 1], ordered[i]
        if curr.games_played is None or prev.games_played is None:
            continue
        delta = curr.games_played - prev.games_played
        if delta <= 0:
            continue
        wins = max(0, (curr.games_won or 0) - (prev.games_won or 0))
        kda_d = round(curr.kda - prev.kda, 2) if curr.kda is not None and prev.kda is not None else None
        end_time = curr.fetched_at if curr.fetched_at.tzinfo else curr.fetched_at.replace(tzinfo=timezone.utc)
        sessions.append({
            "games": delta, "wins": wins, "losses": delta - wins,
            "win_rate": wins / delta * 100, "kda_delta": kda_d, "end_time": end_time,
        })
        if len(sessions) >= limit:
            break
    return sessions


def _all_hll_sessions(snaps, limit: int = 10) -> list[dict]:
    ordered = list(reversed(snaps))
    sessions = []
    for i in range(len(ordered) - 1, 0, -1):
        prev, curr = ordered[i - 1], ordered[i]
        pgd, cgd = prev.game_data or {}, curr.game_data or {}
        prev_pt, curr_pt = pgd.get("playtime_forever"), cgd.get("playtime_forever")
        if prev_pt is None or curr_pt is None:
            continue
        delta_minutes = curr_pt - prev_pt
        if delta_minutes <= 0:
            continue
        kills_d = (cgd["kills"] - pgd["kills"]) if cgd.get("kills") is not None and pgd.get("kills") is not None else None
        xp_d = (cgd["total_xp"] - pgd["total_xp"]) if cgd.get("total_xp") is not None and pgd.get("total_xp") is not None else None
        end_time = curr.fetched_at if curr.fetched_at.tzinfo else curr.fetched_at.replace(tzinfo=timezone.utc)
        sessions.append({"duration_minutes": delta_minutes, "kills_delta": kills_d, "xp_delta": xp_d, "end_time": end_time})
        if len(sessions) >= limit:
            break
    return sessions


def _last_ow_session(snaps) -> dict | None:
    s = _all_ow_sessions(snaps, limit=1)
    return s[0] if s else None


def _last_hll_session(snaps) -> dict | None:
    s = _all_hll_sessions(snaps, limit=1)
    return s[0] if s else None


# ---------------------------------------------------------------------------
# Embed builders — Overwatch
# ---------------------------------------------------------------------------

def build_ow_stats_embed(player: Player, snapshot: StatSnapshot, snaps=None) -> discord.Embed:
    name = player.display_name or player.battletag
    embed = discord.Embed(title=name, color=OW_COLOR)
    embed.set_author(name=f"Overwatch 2 · {player.battletag}")
    if player.avatar_url:
        embed.set_thumbnail(url=player.avatar_url)

    rank_lines = [
        f"**Tank:** {_rank_display(snapshot.rank_tank)}",
        f"**Damage:** {_rank_display(snapshot.rank_damage)}",
        f"**Support:** {_rank_display(snapshot.rank_support)}",
        f"**Open Queue:** {_rank_display(snapshot.rank_open)}",
    ]
    embed.add_field(name="Competitive Ranks", value="\n".join(rank_lines), inline=True)

    stat_lines = []
    if snapshot.win_rate is not None:
        stat_lines.append(f"**Win Rate:** {snapshot.win_rate:.1%}")
    if snapshot.kda is not None:
        stat_lines.append(f"**KDA:** {snapshot.kda:.2f}")
    if snapshot.games_played is not None:
        w = snapshot.games_won or 0
        l = snapshot.games_lost or 0
        stat_lines.append(f"**Games:** {snapshot.games_played} ({w}W / {l}L)")
    if stat_lines:
        embed.add_field(name="Overall Stats", value="\n".join(stat_lines), inline=True)

    sgm = snapshot.stats_by_gamemode or {}
    comp = sgm.get("competitive") or {}
    qp = sgm.get("quickplay") or {}
    mode_lines = []
    if comp.get("games_played"):
        wr = f" · {comp['win_rate']:.0%}" if comp.get("win_rate") is not None else ""
        kda = f" · {comp['kda']:.2f} KDA" if comp.get("kda") is not None else ""
        mode_lines.append(f"**Comp:** {comp['games_played']}g{wr}{kda}")
    if qp.get("games_played"):
        wr = f" · {qp['win_rate']:.0%}" if qp.get("win_rate") is not None else ""
        kda = f" · {qp['kda']:.2f} KDA" if qp.get("kda") is not None else ""
        mode_lines.append(f"**QP:** {qp['games_played']}g{wr}{kda}")
    if mode_lines:
        embed.add_field(name="Gamemode", value="\n".join(mode_lines), inline=True)

    if snaps and len(snaps) >= 2:
        trend = _ow_trend(snaps)
        if trend:
            trend_lines = []
            if trend["games_delta"] is not None:
                sign = "+" if trend["games_delta"] >= 0 else ""
                trend_lines.append(f"**Games:** {sign}{trend['games_delta']}")
            if trend["wr_delta"] is not None:
                sign = "+" if trend["wr_delta"] >= 0 else ""
                trend_lines.append(f"**Win Rate:** {sign}{trend['wr_delta']}%")
            if trend["kda_delta"] is not None:
                sign = "+" if trend["kda_delta"] >= 0 else ""
                trend_lines.append(f"**KDA:** {sign}{trend['kda_delta']}")
            if trend_lines:
                embed.add_field(name=f"{trend['period']}D Trend", value="\n".join(trend_lines), inline=True)

        session = _last_ow_session(snaps)
        if session:
            game_word = "game" if session["games"] == 1 else "games"
            kda_str = ""
            if session["kda_delta"] is not None:
                sign = "+" if session["kda_delta"] >= 0 else ""
                kda_str = f"\n**KDA:** {sign}{session['kda_delta']} · {_discord_timeago(session['end_time'])}"
            session_lines = (
                f"{session['wins']}W / {session['losses']}L ({session['games']} {game_word})\n"
                f"**Win Rate:** {session['win_rate']:.1f}%"
                f"{kda_str}"
            )
            embed.add_field(name="Last Session", value=session_lines, inline=True)

    if snapshot.top_heroes:
        hero_lines = []
        for h in snapshot.top_heroes[:3]:
            time_str = _fmt_time(h.get("time_played", 0))
            wr = h.get("win_rate")
            wr_str = f" · {wr:.0%} WR" if wr is not None else ""
            kda = h.get("kda")
            kda_str = f" · {kda:.2f} KDA" if kda is not None else ""
            hero_lines.append(f"**{h.get('name', h.get('hero', '?'))}:** {time_str}{wr_str}{kda_str}")
        embed.add_field(name="Top Heroes", value="\n".join(hero_lines), inline=False)

    fa = snapshot.fetched_at
    fa_str = fa.strftime("%Y-%m-%d %H:%M UTC")
    fa_utc = fa if fa.tzinfo else fa.replace(tzinfo=timezone.utc)
    embed.set_footer(text=f"Last updated · {fa_str} ({_discord_timeago(fa_utc)})")
    return embed


# Keep name alias used elsewhere
def build_stats_embed(player: Player, snapshot: StatSnapshot, snaps=None) -> discord.Embed:
    if player.game == "hell_let_loose":
        return build_hll_stats_embed(player, snapshot, snaps)
    return build_ow_stats_embed(player, snapshot, snaps)


def build_game_report_embed(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> discord.Embed:
    games_delta = (new["games_played"] or 0) - (prev["games_played"] or 0)
    wins_delta = (new["games_won"] or 0) - (prev["games_won"] or 0)
    losses_delta = games_delta - wins_delta

    if wins_delta > losses_delta:
        color = WIN_COLOR
    elif losses_delta > wins_delta:
        color = LOSS_COLOR
    else:
        color = TIE_COLOR

    embed = discord.Embed(title=f"📊 Session Summary — {player_name}", color=color)
    embed.set_author(name=f"Overwatch 2 · {battletag}")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    result_parts = []
    if wins_delta:
        result_parts.append(f"✅ **{wins_delta}W**")
    if losses_delta:
        result_parts.append(f"❌ **{losses_delta}L**")
    session_str = "  ·  ".join(result_parts) if result_parts else "—"
    game_word = "game" if games_delta == 1 else "games"
    embed.add_field(name=f"Session ({games_delta} {game_word})", value=session_str, inline=False)

    rank_changes = []
    for label, key in [("Tank", "rank_tank"), ("Damage", "rank_damage"),
                        ("Support", "rank_support"), ("Open Queue", "rank_open")]:
        p, n = prev[key], new[key]
        if p != n:
            rank_changes.append(f"**{label}:** {_rank_display(p) if p else 'Unranked'} → {_rank_display(n) if n else 'Unranked'}")
    if rank_changes:
        embed.add_field(name="Rank Changes", value="\n".join(rank_changes), inline=False)

    stat_lines = []
    if new["win_rate"] is not None:
        prev_wr = prev.get("win_rate")
        if prev_wr is not None:
            delta = new["win_rate"] - prev_wr
            sign = "+" if delta >= 0 else ""
            stat_lines.append(f"**Win Rate:** {new['win_rate']:.1%} ({sign}{delta:.1%})")
        else:
            stat_lines.append(f"**Win Rate:** {new['win_rate']:.1%}")
    if new["kda"] is not None:
        prev_kda = prev.get("kda")
        if prev_kda is not None:
            delta = new["kda"] - prev_kda
            sign = "+" if delta >= 0 else ""
            stat_lines.append(f"**KDA:** {new['kda']:.2f} ({sign}{delta:.2f})")
        else:
            stat_lines.append(f"**KDA:** {new['kda']:.2f}")
    if new["games_played"] is not None:
        stat_lines.append(f"**Career Games:** {new['games_played']}")
    if stat_lines:
        embed.add_field(name="Overall Stats", value="\n".join(stat_lines), inline=True)

    fetched_at: datetime = new["fetched_at"]
    embed.set_footer(text=f"Session finalised · {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


def build_stats_update_embed(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> discord.Embed:
    embed = discord.Embed(title=f"📈 Stats Updated — {player_name}", color=OW_COLOR)
    embed.set_author(name=f"Overwatch 2 · {battletag}")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    rank_changes = []
    for label, key in [("Tank", "rank_tank"), ("Damage", "rank_damage"),
                        ("Support", "rank_support"), ("Open Queue", "rank_open")]:
        p, n = prev.get(key), new.get(key)
        if p != n:
            rank_changes.append(
                f"**{label}:** {_rank_display(p) if p else 'Unranked'} → {_rank_display(n) if n else 'Unranked'}"
            )
    if rank_changes:
        embed.add_field(name="Rank Changes", value="\n".join(rank_changes), inline=False)

    stat_lines = []
    if new.get("win_rate") is not None:
        prev_wr = prev.get("win_rate")
        if prev_wr is not None and prev_wr != new["win_rate"]:
            delta = new["win_rate"] - prev_wr
            sign = "+" if delta >= 0 else ""
            stat_lines.append(f"**Win Rate:** {new['win_rate']:.1%} ({sign}{delta:.1%})")
        else:
            stat_lines.append(f"**Win Rate:** {new['win_rate']:.1%}")
    if new.get("kda") is not None:
        prev_kda = prev.get("kda")
        if prev_kda is not None and prev_kda != new["kda"]:
            delta = new["kda"] - prev_kda
            sign = "+" if delta >= 0 else ""
            stat_lines.append(f"**KDA:** {new['kda']:.2f} ({sign}{delta:.2f})")
        else:
            stat_lines.append(f"**KDA:** {new['kda']:.2f}")
    if new.get("games_played") is not None:
        stat_lines.append(f"**Career Games:** {new['games_played']}")
    if stat_lines:
        embed.add_field(name="Current Stats", value="\n".join(stat_lines), inline=True)

    fetched_at: datetime = new["fetched_at"]
    embed.set_footer(text=f"Detected · {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


# ---------------------------------------------------------------------------
# Embed builders — Hell Let Loose
# ---------------------------------------------------------------------------

def build_hll_stats_embed(player: Player, snapshot: StatSnapshot, snaps=None) -> discord.Embed:
    name = player.display_name or player.battletag
    embed = discord.Embed(title=name, color=HLL_COLOR)
    embed.set_author(name=f"Hell Let Loose · {player.battletag}")
    if player.avatar_url:
        embed.set_thumbnail(url=player.avatar_url)

    gd = snapshot.game_data or {}

    # Combat stats
    combat_lines = []
    if gd.get("kills") is not None:
        kills = gd["kills"]
        hs = gd.get("headshots") or 0
        hs_pct = f" ({hs / kills:.0%} HS)" if kills > 0 else ""
        combat_lines.append(f"**Kills:** {kills:,}{hs_pct}")
    if gd.get("tank_kills") or gd.get("vehicle_kills"):
        combat_lines.append(
            f"**Vehicles:** {(gd.get('tank_kills') or 0) + (gd.get('vehicle_kills') or 0):,}"
        )
    if gd.get("sector_caps"):
        combat_lines.append(f"**Sector Caps:** {gd['sector_caps']:,}")
    if combat_lines:
        embed.add_field(name="Combat", value="\n".join(combat_lines), inline=True)

    # Playtime + top role
    info_lines = []
    pt = gd.get("playtime_forever")
    if pt is not None:
        h, m = divmod(pt, 60)
        info_lines.append(f"**Playtime:** {h}h {m}m")
    if gd.get("top_role"):
        info_lines.append(f"**Top Role:** {gd['top_role']}")
    if info_lines:
        embed.add_field(name="Profile", value="\n".join(info_lines), inline=True)

    if snaps and len(snaps) >= 2:
        trend = _hll_trend(snaps)
        if trend:
            trend_lines = []
            if trend["kills_delta"] is not None:
                sign = "+" if trend["kills_delta"] >= 0 else ""
                trend_lines.append(f"**Kills:** {sign}{trend['kills_delta']:,}")
            if trend["pt_delta"] is not None:
                sign = "+" if trend["pt_delta"] >= 0 else ""
                th, tm = divmod(abs(trend["pt_delta"]), 60)
                pt_str = f"{th}h {tm}m" if th else f"{tm}m"
                trend_lines.append(f"**Playtime:** {sign}{pt_str}")
            if trend_lines:
                embed.add_field(name=f"{trend['period']}D Trend", value="\n".join(trend_lines), inline=True)

        session = _last_hll_session(snaps)
        if session:
            sh, sm = divmod(session["duration_minutes"], 60)
            dur_str = f"{sh}h {sm}m" if sh else f"{sm}m"
            sess_lines = [f"**Duration:** {dur_str}"]
            if session["kills_delta"] is not None:
                sess_lines.append(f"**Kills:** +{session['kills_delta']:,}")
            if session["xp_delta"] is not None and session["xp_delta"] > 0:
                sess_lines.append(f"**XP:** +{session['xp_delta']:,}  ·  {_discord_timeago(session['end_time'])}")
            elif sess_lines:
                sess_lines[-1] += f"  ·  {_discord_timeago(session['end_time'])}"
            embed.add_field(name="Last Session", value="\n".join(sess_lines), inline=True)

    fa = snapshot.fetched_at
    fa_str = fa.strftime("%Y-%m-%d %H:%M UTC")
    fa_utc = fa if fa.tzinfo else fa.replace(tzinfo=timezone.utc)
    embed.set_footer(text=f"Last updated · {fa_str} ({_discord_timeago(fa_utc)})")
    return embed


def build_hll_session_embed(
    player_name: str,
    steam_id: str,
    avatar_url: str | None,
    duration_minutes: int,
    kills_delta: int | None = None,
    headshots_delta: int | None = None,
    sector_caps_delta: int | None = None,
    xp_delta: int | None = None,
    top_role: str | None = None,
) -> discord.Embed:
    h, m = divmod(duration_minutes, 60)
    duration_str = f"{h}h {m}m" if h else f"{m}m"

    embed = discord.Embed(title=f"🎖 Session Ended — {player_name}", color=HLL_COLOR)
    embed.set_author(name=f"Hell Let Loose · {steam_id}")
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    # Session summary field
    summary_lines = [f"⏱ **{duration_str}**"]
    if top_role:
        summary_lines.append(f"🎭 **Role:** {top_role}")
    embed.add_field(name="Session", value="\n".join(summary_lines), inline=False)

    # Combat stats
    combat_lines = []
    if kills_delta is not None:
        hs_str = ""
        if headshots_delta is not None and kills_delta > 0:
            hs_str = f" ({headshots_delta / kills_delta:.0%} HS)"
        combat_lines.append(f"**Kills:** {kills_delta:,}{hs_str}")
    if sector_caps_delta is not None and sector_caps_delta > 0:
        combat_lines.append(f"**Sector Caps:** {sector_caps_delta:,}")
    if combat_lines:
        embed.add_field(name="Combat", value="\n".join(combat_lines), inline=True)

    # XP gained
    if xp_delta is not None and xp_delta > 0:
        embed.add_field(name="XP Gained", value=f"{xp_delta:,}", inline=True)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Session finalised · {now_str}")
    return embed


# ---------------------------------------------------------------------------
# Embed builder — compare
# ---------------------------------------------------------------------------

def build_compare_embed(p1: Player, s1: StatSnapshot, p2: Player, s2: StatSnapshot) -> discord.Embed:
    color = OW_COLOR if p1.game == "overwatch" else HLL_COLOR
    n1 = p1.display_name or p1.battletag.split("#")[0]
    n2 = p2.display_name or p2.battletag.split("#")[0]
    embed = discord.Embed(title=f"{n1} vs {n2}", color=color)

    if p1.game == "overwatch":
        embed.set_author(name="Overwatch 2 — Player Comparison")

        def _ow_vals(s: StatSnapshot) -> str:
            lines = [
                f"{s.win_rate:.1%}" if s.win_rate is not None else "—",
                f"{s.kda:.2f}" if s.kda is not None else "—",
                str(s.games_played) if s.games_played is not None else "—",
                _rank_display(s.rank_tank),
                _rank_display(s.rank_damage),
                _rank_display(s.rank_support),
                _rank_display(s.rank_open),
            ]
            return "\n".join(lines)

        embed.add_field(name="Stat", value="Win Rate\nKDA\nGames\nTank\nDamage\nSupport\nOpen", inline=True)
        embed.add_field(name=n1, value=_ow_vals(s1), inline=True)
        embed.add_field(name=n2, value=_ow_vals(s2), inline=True)
    else:
        embed.set_author(name="Hell Let Loose — Player Comparison")

        def _hll_vals(s: StatSnapshot) -> str:
            gd = s.game_data or {}
            k = gd.get("kills")
            hs = gd.get("headshots") or 0
            kills_str = f"{k:,} ({hs/k:.0%} HS)" if k else "—"
            pt = gd.get("playtime_forever")
            if pt is not None:
                h, m = divmod(pt, 60)
                pt_str = f"{h}h {m}m"
            else:
                pt_str = "—"
            lines = [kills_str, pt_str, gd.get("top_role") or "—"]
            return "\n".join(lines)

        embed.add_field(name="Stat", value="Kills\nPlaytime\nTop Role", inline=True)
        embed.add_field(name=n1, value=_hll_vals(s1), inline=True)
        embed.add_field(name=n2, value=_hll_vals(s2), inline=True)

    return embed


# ---------------------------------------------------------------------------
# Notification dispatch (called by scheduler)
# ---------------------------------------------------------------------------

async def _broadcast(embed: discord.Embed, game: str | None = None) -> None:
    """Send embed to all registered channels that match the given game filter.
    Channels with game=NULL receive all games; channels with a specific game only
    receive notifications for that game.
    """
    async with AsyncSessionLocal() as session:
        if game:
            result = await session.execute(
                select(DiscordChannel).where(
                    or_(DiscordChannel.game == None, DiscordChannel.game == game)  # noqa: E711
                )
            )
        else:
            result = await session.execute(select(DiscordChannel))
        channels = result.scalars().all()

    for ch in channels:
        discord_channel = bot.get_channel(int(ch.channel_id))
        if discord_channel:
            try:
                await discord_channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("No permission to send to channel %s", ch.channel_id)
            except Exception as e:
                logger.error("Failed to send to channel %s: %s", ch.channel_id, e)


async def send_game_report(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> None:
    if not bot.is_ready():
        logger.warning("Bot not ready — queuing game report for %s", _slog(battletag))
        _notification_queue.append(lambda: send_game_report(player_name, battletag, avatar_url, prev, new))
        return
    embed = build_game_report_embed(player_name, battletag, avatar_url, prev, new)
    await _broadcast(embed, game="overwatch")


async def send_stats_update(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> None:
    if not bot.is_ready():
        logger.warning("Bot not ready — queuing stats update for %s", _slog(battletag))
        _notification_queue.append(lambda: send_stats_update(player_name, battletag, avatar_url, prev, new))
        return
    embed = build_stats_update_embed(player_name, battletag, avatar_url, prev, new)
    await _broadcast(embed, game="overwatch")


async def send_hll_session_report(
    player_name: str,
    steam_id: str,
    avatar_url: str | None,
    duration_minutes: int,
    kills_delta: int | None = None,
    headshots_delta: int | None = None,
    sector_caps_delta: int | None = None,
    xp_delta: int | None = None,
    top_role: str | None = None,
) -> None:
    if not bot.is_ready():
        logger.warning("Bot not ready — queuing HLL session report for %s", _slog(steam_id))
        _notification_queue.append(lambda: send_hll_session_report(
            player_name, steam_id, avatar_url, duration_minutes,
            kills_delta, headshots_delta, sector_caps_delta, xp_delta, top_role,
        ))
        return
    embed = build_hll_session_embed(
        player_name, steam_id, avatar_url, duration_minutes,
        kills_delta, headshots_delta, sector_caps_delta, xp_delta, top_role,
    )
    await _broadcast(embed, game="hell_let_loose")


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        logger.info("Discord bot ready as %s — synced %d slash commands", bot.user, len(synced))
    except Exception as e:
        logger.error("Failed to sync slash commands: %s", e)
    await _flush_notification_queue()


@bot.event
async def on_resumed():
    logger.info("Discord session resumed")
    await _flush_notification_queue()


# ---------------------------------------------------------------------------
# Autocomplete helpers
# ---------------------------------------------------------------------------

async def _tracked_players_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).order_by(Player.battletag))
        players = result.scalars().all()

    choices = []
    for p in players:
        display = p.display_name or p.battletag.split("#")[0]
        game_tag = "[HLL] " if p.game == "hell_let_loose" else "[OW] "
        label = f"{game_tag}{display} ({p.battletag})"
        if not current or current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=p.battletag))

    return choices[:25]


async def _ow_players_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Player).where(Player.game == "overwatch").order_by(Player.battletag)
        )
        players = result.scalars().all()

    choices = []
    for p in players:
        display = p.display_name or p.battletag.split("#")[0]
        label = f"{display} ({p.battletag})"
        if not current or current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=p.battletag))
    return choices[:25]


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

_GAME_CHOICES = [
    app_commands.Choice(name="Overwatch 2", value="overwatch"),
    app_commands.Choice(name="Hell Let Loose", value="hell_let_loose"),
]

_GAME_CHOICES_WITH_ALL = [
    app_commands.Choice(name="All games",          value="all"),
    app_commands.Choice(name="Overwatch 2",        value="overwatch"),
    app_commands.Choice(name="Hell Let Loose",     value="hell_let_loose"),
]

_LEADERBOARD_STAT_CHOICES = [
    app_commands.Choice(name="KDA",            value="kda"),
    app_commands.Choice(name="Win Rate",       value="win_rate"),
    app_commands.Choice(name="Games",          value="games"),
    app_commands.Choice(name="Kills (HLL)",    value="kills"),
    app_commands.Choice(name="Playtime (HLL)", value="playtime"),
]


@bot.tree.command(name="add_player", description="Start tracking a player")
@app_commands.describe(
    player_id="BattleTag (e.g. Username#1234) for OW, or Steam64 ID for HLL",
    game="Which game to track (default: Overwatch 2)",
)
@app_commands.choices(game=_GAME_CHOICES)
async def cmd_add_player(
    interaction: discord.Interaction,
    player_id: str,
    game: app_commands.Choice[str] = None,
):
    await interaction.response.defer()
    player_id = player_id.strip()
    game_value = game.value if game else "overwatch"

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == player_id))
        if result.scalar_one_or_none():
            await interaction.followup.send(f"**{player_id}** is already being tracked.", ephemeral=True)
            return

    if game_value == "hell_let_loose":
        await _add_hll_player(interaction, player_id)
    else:
        await _add_ow_player(interaction, player_id)


async def _add_ow_player(interaction: discord.Interaction, battletag: str) -> None:
    from ow_client import fetch_player, PlayerNotFoundError, ProfilePrivateError, OverFastError, InvalidBattletagError
    try:
        data = await fetch_player(battletag)
    except InvalidBattletagError:
        await interaction.followup.send(
            f"`{battletag}` is not a valid battletag. Format should be `Username#1234`.", ephemeral=True
        )
        return
    except PlayerNotFoundError:
        await interaction.followup.send(
            f"Player `{battletag}` not found. Format should be `Username#1234`.", ephemeral=True
        )
        return
    except ProfilePrivateError:
        await interaction.followup.send(f"**{battletag}**'s profile is private.", ephemeral=True)
        return
    except OverFastError as e:
        await interaction.followup.send(f"API error fetching `{battletag}`: {e}", ephemeral=True)
        return

    async with AsyncSessionLocal() as session:
        player = Player(battletag=battletag, game="overwatch", display_name=data.username, avatar_url=data.avatar)
        session.add(player)
        await session.flush()

        snapshot = StatSnapshot(
            player_id=player.id,
            fetched_at=datetime.now(timezone.utc),
            rank_tank=data.rank_tank,
            rank_damage=data.rank_damage,
            rank_support=data.rank_support,
            rank_open=data.rank_open,
            games_played=data.games_played,
            games_won=data.games_won,
            games_lost=data.games_lost,
            kda=data.kda,
            win_rate=data.win_rate,
            top_heroes=[
                {"hero": h.hero, "name": h.name, "time_played": h.time_played,
                 "win_rate": h.win_rate, "kda": h.kda,
                 "damage_per_10_min": h.damage_per_10_min,
                 "healing_per_10_min": h.healing_per_10_min,
                 "eliminations_per_10_min": h.eliminations_per_10_min}
                for h in data.top_heroes
            ],
            stats_by_gamemode=data.stats_by_gamemode,
            raw_summary=data.raw_summary,
            raw_stats=data.raw_stats,
        )
        session.add(snapshot)
        await session.commit()

        embed = build_ow_stats_embed(player, snapshot)
        await interaction.followup.send(f"Now tracking **{battletag}** (Overwatch 2)!", embed=embed)


async def _add_hll_player(interaction: discord.Interaction, steam_id: str) -> None:
    from hll_client import fetch_player as hll_fetch, PlayerNotFoundError, ProfilePrivateError, HLLClientError
    api_key = os.getenv("STEAM_API_KEY", "")
    if not api_key:
        await interaction.followup.send("Steam API key not configured — set `STEAM_API_KEY` in .env.", ephemeral=True)
        return
    try:
        data = await hll_fetch(steam_id, api_key)
    except PlayerNotFoundError:
        await interaction.followup.send(
            f"Steam ID `{steam_id}` not found. Make sure you're using the 17-digit Steam64 ID.",
            ephemeral=True,
        )
        return
    except ProfilePrivateError:
        await interaction.followup.send(
            f"Steam profile for `{steam_id}` is private.\n"
            "The player must set their Steam profile visibility to **Public** (including Game details).",
            ephemeral=True,
        )
        return
    except HLLClientError as e:
        await interaction.followup.send(f"Steam API error: {e}", ephemeral=True)
        return

    async with AsyncSessionLocal() as session:
        player = Player(battletag=steam_id, game="hell_let_loose", display_name=data.display_name, avatar_url=data.avatar)
        session.add(player)
        await session.flush()

        game_data = {
            "playtime_forever": data.playtime_forever,
            "playtime_2weeks": data.playtime_2weeks,
        }
        snapshot = StatSnapshot(
            player_id=player.id,
            fetched_at=datetime.now(timezone.utc),
            game_data=game_data,
        )
        session.add(snapshot)
        await session.commit()

        embed = build_hll_stats_embed(player, snapshot)
        await interaction.followup.send(f"Now tracking **{data.display_name}** (Hell Let Loose)!", embed=embed)


@bot.tree.command(name="remove_player", description="Stop tracking a player")
@app_commands.describe(player_id="Player identifier (autocompletes tracked players)")
@app_commands.autocomplete(player_id=_tracked_players_autocomplete)
async def cmd_remove_player(interaction: discord.Interaction, player_id: str):
    player_id = player_id.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == player_id))
        player = result.scalar_one_or_none()
        if not player:
            await interaction.response.send_message(
                f"**{player_id}** is not currently tracked.", ephemeral=True
            )
            return
        name = player.display_name or player_id
        await session.delete(player)
        await session.commit()
    await interaction.response.send_message(f"Stopped tracking **{name}**.")


@bot.tree.command(name="stats", description="Show stats for a tracked player")
@app_commands.describe(player_id="Player identifier — autocompletes tracked players")
@app_commands.autocomplete(player_id=_tracked_players_autocomplete)
async def cmd_stats(interaction: discord.Interaction, player_id: str):
    await interaction.response.defer()
    player_id = player_id.strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == player_id))
        player = result.scalar_one_or_none()
        if player:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            snap_result = await session.execute(
                select(StatSnapshot)
                .where(StatSnapshot.player_id == player.id)
                .where(StatSnapshot.fetched_at >= cutoff)
                .order_by(StatSnapshot.fetched_at.desc())
            )
            snaps = snap_result.scalars().all()
            if snaps:
                embed = build_stats_embed(player, snaps[0], snaps)
                await interaction.followup.send(embed=embed)
                return

    # For OW, allow live lookup of untracked players
    if "#" in player_id:
        from ow_client import fetch_player, PlayerNotFoundError, ProfilePrivateError, OverFastError, InvalidBattletagError
        try:
            data = await fetch_player(player_id)
        except InvalidBattletagError:
            await interaction.followup.send(
                f"`{player_id}` is not a valid battletag. Format: `Username#1234`.", ephemeral=True
            )
            return
        except PlayerNotFoundError:
            await interaction.followup.send(
                f"Player `{player_id}` not found. Format: `Username#1234`.", ephemeral=True
            )
            return
        except ProfilePrivateError:
            await interaction.followup.send(f"**{player_id}**'s profile is private.", ephemeral=True)
            return
        except OverFastError as e:
            await interaction.followup.send(f"API error for `{player_id}`: {e}", ephemeral=True)
            return

        temp_player = Player(battletag=player_id, game="overwatch", display_name=data.username, avatar_url=data.avatar)
        temp_snapshot = StatSnapshot(
            player_id=0,
            fetched_at=datetime.now(timezone.utc),
            rank_tank=data.rank_tank, rank_damage=data.rank_damage,
            rank_support=data.rank_support, rank_open=data.rank_open,
            games_played=data.games_played, games_won=data.games_won,
            games_lost=data.games_lost, kda=data.kda, win_rate=data.win_rate,
            top_heroes=[{"hero": h.hero, "name": h.name, "time_played": h.time_played,
                          "win_rate": h.win_rate, "kda": h.kda} for h in data.top_heroes],
        )
        embed = build_ow_stats_embed(temp_player, temp_snapshot)
        embed.set_footer(text=f"Live fetch (not tracked) · {temp_snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(
            f"**{player_id}** is not tracked. Add them first with `/add_player`.", ephemeral=True
        )


@bot.tree.command(name="players", description="List all currently tracked players")
async def cmd_players(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).order_by(Player.game, Player.added_at))
        players = result.scalars().all()

        last_seen: dict[int, datetime] = {}
        for p in players:
            snap_result = await session.execute(
                select(StatSnapshot.fetched_at)
                .where(StatSnapshot.player_id == p.id)
                .order_by(StatSnapshot.fetched_at.desc())
                .limit(1)
            )
            fa = snap_result.scalar_one_or_none()
            if fa is not None:
                last_seen[p.id] = fa

    if not players:
        await interaction.response.send_message(
            "No players tracked yet. Use `/add_player` to get started."
        )
        return

    ow_players = [p for p in players if p.game == "overwatch"]
    hll_players = [p for p in players if p.game == "hell_let_loose"]

    def _player_line(p: Player) -> str:
        fa = last_seen.get(p.id)
        ago = f" · {_discord_timeago(fa)}" if fa else ""
        return f"• **{p.display_name or p.battletag}** (`{p.battletag}`){ago}"

    embed = discord.Embed(title=f"Tracked Players ({len(players)})", color=OW_COLOR)
    if ow_players:
        embed.add_field(name="Overwatch 2", value="\n".join(_player_line(p) for p in ow_players), inline=False)
    if hll_players:
        embed.add_field(name="Hell Let Loose", value="\n".join(_player_line(p) for p in hll_players), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Rank tracked players by a stat")
@app_commands.describe(
    game="Filter by game (default: Overwatch 2)",
    stat="Stat to rank by",
)
@app_commands.choices(game=_GAME_CHOICES_WITH_ALL, stat=_LEADERBOARD_STAT_CHOICES)
async def cmd_leaderboard(
    interaction: discord.Interaction,
    game: app_commands.Choice[str] = None,
    stat: app_commands.Choice[str] = None,
):
    await interaction.response.defer()
    game_filter = game.value if game else "overwatch"
    stat_key = stat.value if stat else None

    async with AsyncSessionLocal() as session:
        if game_filter == "all":
            result = await session.execute(select(Player).order_by(Player.game, Player.battletag))
        else:
            result = await session.execute(
                select(Player).where(Player.game == game_filter).order_by(Player.battletag)
            )
        players = result.scalars().all()

        if not players:
            await interaction.followup.send("No players tracked yet.", ephemeral=True)
            return

        entries: list[tuple[Player, StatSnapshot]] = []
        for p in players:
            snap_result = await session.execute(
                select(StatSnapshot)
                .where(StatSnapshot.player_id == p.id)
                .order_by(StatSnapshot.fetched_at.desc())
                .limit(1)
            )
            snap = snap_result.scalar_one_or_none()
            if snap:
                entries.append((p, snap))

    if not entries:
        await interaction.followup.send("No snapshot data found.", ephemeral=True)
        return

    def _stat_value(p: Player, s: StatSnapshot) -> float:
        effective_stat = stat_key
        if effective_stat is None:
            effective_stat = "kills" if p.game == "hell_let_loose" else "kda"
        if effective_stat == "kda":
            return s.kda or 0.0
        if effective_stat == "win_rate":
            return s.win_rate or 0.0
        if effective_stat == "games":
            return float(s.games_played or 0)
        gd = s.game_data or {}
        if effective_stat == "kills":
            return float(gd.get("kills") or 0)
        if effective_stat == "playtime":
            return float(gd.get("playtime_forever") or 0)
        return 0.0

    def _stat_label(p: Player, s: StatSnapshot) -> str:
        effective_stat = stat_key
        if effective_stat is None:
            effective_stat = "kills" if p.game == "hell_let_loose" else "kda"
        if effective_stat == "kda":
            return f"{s.kda:.2f} KDA" if s.kda is not None else "—"
        if effective_stat == "win_rate":
            return f"{s.win_rate:.1%} WR" if s.win_rate is not None else "—"
        if effective_stat == "games":
            if s.games_played is None:
                return "—"
            w = s.games_won or 0
            l = s.games_lost or 0
            return f"{s.games_played:,} ({w}W / {l}L)"
        gd = s.game_data or {}
        if effective_stat == "kills":
            k = gd.get("kills")
            return f"{k:,} kills" if k is not None else "—"
        if effective_stat == "playtime":
            pt = gd.get("playtime_forever")
            if pt is None:
                return "—"
            h, m = divmod(pt, 60)
            return f"{h}h {m}m"
        return "—"

    entries.sort(key=lambda e: _stat_value(e[0], e[1]), reverse=True)

    stat_name = stat.name if stat else "KDA / Kills"
    game_name = game.name if game else "Overwatch 2"
    embed_color = HLL_COLOR if game_filter == "hell_let_loose" else OW_COLOR
    embed = discord.Embed(title=f"Leaderboard — {game_name} · {stat_name}", color=embed_color)

    lines = []
    for rank, (p, s) in enumerate(entries, 1):
        name = p.display_name or p.battletag.split("#")[0]
        game_tag = "[HLL] " if p.game == "hell_let_loose" else ""
        lines.append(f"**#{rank}** {game_tag}{name} — {_stat_label(p, s)}")

    text = "\n".join(lines)
    if len(text) > 1024:
        text = text[:1021] + "..."
    embed.add_field(name="​", value=text, inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="sessions", description="Show recent sessions for a tracked player")
@app_commands.describe(
    player_id="Player identifier",
    count="Number of sessions to show (1–10, default 5)",
)
@app_commands.autocomplete(player_id=_tracked_players_autocomplete)
async def cmd_sessions(interaction: discord.Interaction, player_id: str, count: int = 5):
    await interaction.response.defer()
    count = max(1, min(count, 10))
    player_id = player_id.strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == player_id))
        player = result.scalar_one_or_none()
        if not player:
            await interaction.followup.send(f"**{player_id}** is not tracked.", ephemeral=True)
            return

        cutoff = datetime.now(timezone.utc) - timedelta(days=60)
        snap_result = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .where(StatSnapshot.fetched_at >= cutoff)
            .order_by(StatSnapshot.fetched_at.desc())
        )
        snaps = snap_result.scalars().all()

    name = player.display_name or player.battletag

    if player.game == "overwatch":
        sessions = _all_ow_sessions(snaps, limit=count)
        if not sessions:
            await interaction.followup.send(
                f"No sessions found for **{name}** in the last 60 days.", ephemeral=True
            )
            return
        lines = []
        for i, s in enumerate(sessions, 1):
            game_word = "game" if s["games"] == 1 else "games"
            kda_str = ""
            if s["kda_delta"] is not None:
                sign = "+" if s["kda_delta"] >= 0 else ""
                kda_str = f" · KDA {sign}{s['kda_delta']}"
            lines.append(
                f"**{i}.** {s['wins']}W / {s['losses']}L ({s['games']} {game_word})"
                f" · {s['win_rate']:.1f}% WR{kda_str} · {_discord_timeago(s['end_time'])}"
            )
        embed = discord.Embed(title=f"Sessions — {name}", color=OW_COLOR)
        embed.set_author(name=f"Overwatch 2 · {player.battletag}")
    else:
        sessions = _all_hll_sessions(snaps, limit=count)
        if not sessions:
            await interaction.followup.send(
                f"No sessions found for **{name}** in the last 60 days.", ephemeral=True
            )
            return
        lines = []
        for i, s in enumerate(sessions, 1):
            h, m = divmod(s["duration_minutes"], 60)
            dur_str = f"{h}h {m}m" if h else f"{m}m"
            parts = [f"**{i}.** {dur_str}"]
            if s["kills_delta"] is not None:
                parts.append(f"+{s['kills_delta']:,} kills")
            if s["xp_delta"] is not None and s["xp_delta"] > 0:
                parts.append(f"+{s['xp_delta']:,} XP")
            parts.append(_discord_timeago(s["end_time"]))
            lines.append(" · ".join(parts))
        embed = discord.Embed(title=f"Sessions — {name}", color=HLL_COLOR)
        embed.set_author(name=f"Hell Let Loose · {player.battletag}")

    text = "\n".join(lines)
    embed.add_field(name=f"Last {len(sessions)} session(s)", value=text, inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="compare", description="Compare two tracked players side-by-side")
@app_commands.describe(player1="First player", player2="Second player")
@app_commands.autocomplete(player1=_tracked_players_autocomplete, player2=_tracked_players_autocomplete)
async def cmd_compare(interaction: discord.Interaction, player1: str, player2: str):
    await interaction.response.defer()
    player1, player2 = player1.strip(), player2.strip()

    if player1 == player2:
        await interaction.followup.send("Please select two different players.", ephemeral=True)
        return

    async with AsyncSessionLocal() as session:
        r1 = await session.execute(select(Player).where(Player.battletag == player1))
        p1 = r1.scalar_one_or_none()
        r2 = await session.execute(select(Player).where(Player.battletag == player2))
        p2 = r2.scalar_one_or_none()

        if not p1:
            await interaction.followup.send(f"**{player1}** is not tracked.", ephemeral=True)
            return
        if not p2:
            await interaction.followup.send(f"**{player2}** is not tracked.", ephemeral=True)
            return
        if p1.game != p2.game:
            await interaction.followup.send(
                "Both players must be from the same game.", ephemeral=True
            )
            return

        snap_r1 = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == p1.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        s1 = snap_r1.scalar_one_or_none()

        snap_r2 = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == p2.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        s2 = snap_r2.scalar_one_or_none()

        if not s1 or not s2:
            await interaction.followup.send(
                "Could not find snapshot data for one or both players.", ephemeral=True
            )
            return

    embed = build_compare_embed(p1, s1, p2, s2)
    await interaction.followup.send(embed=embed)


_CHANNEL_GAME_CHOICES = [
    app_commands.Choice(name="All games",          value="all"),
    app_commands.Choice(name="Overwatch 2 only",   value="overwatch"),
    app_commands.Choice(name="Hell Let Loose only", value="hell_let_loose"),
]


@bot.tree.command(
    name="set_channel",
    description="Register this channel to receive game notifications",
)
@app_commands.describe(game="Which game's notifications to receive (default: all games)")
@app_commands.choices(game=_CHANNEL_GAME_CHOICES)
async def cmd_set_channel(
    interaction: discord.Interaction,
    game: app_commands.Choice[str] = None,
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True
        )
        return

    channel_name = (
        interaction.channel.name
        if hasattr(interaction.channel, "name")
        else str(interaction.channel_id)
    )
    game_value = None if (game is None or game.value == "all") else game.value
    game_label = game.name if game else "All games"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiscordChannel).where(DiscordChannel.channel_id == str(interaction.channel_id))
        )
        existing = result.scalar_one_or_none()
        if existing:
            # Update the game filter on the existing registration
            existing.game = game_value
            await session.commit()
            await interaction.response.send_message(
                f"✅ **#{channel_name}** updated — now receiving **{game_label}** notifications."
            )
            return

        session.add(DiscordChannel(
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
            channel_name=channel_name,
            game=game_value,
        ))
        await session.commit()

    await interaction.response.send_message(
        f"✅ **#{channel_name}** registered for **{game_label}** notifications."
    )


@bot.tree.command(
    name="remove_channel",
    description="Unregister this channel from game notifications",
)
async def cmd_remove_channel(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiscordChannel).where(DiscordChannel.channel_id == str(interaction.channel_id))
        )
        ch = result.scalar_one_or_none()
        if not ch:
            await interaction.response.send_message(
                "This channel is not registered for notifications.", ephemeral=True
            )
            return
        await session.delete(ch)
        await session.commit()

    await interaction.response.send_message("Channel removed from game notifications.")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def start_bot() -> asyncio.Task | None:
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.warning("DISCORD_BOT_TOKEN not set — Discord bot disabled")
        return None
    task = asyncio.create_task(bot.start(token), name="discord-bot")
    logger.info("Discord bot starting...")
    return task


async def stop_bot() -> None:
    if not bot.is_closed():
        await bot.close()
        logger.info("Discord bot stopped")

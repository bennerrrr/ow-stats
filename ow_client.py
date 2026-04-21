import time
from dataclasses import dataclass, field
from typing import Optional
import httpx

BASE_URL = "https://overfast-api.tekrop.fr"
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 3600  # 1 hour


class ProfilePrivateError(Exception):
    pass


class PlayerNotFoundError(Exception):
    pass


class OverFastError(Exception):
    pass


@dataclass
class HeroStat:
    hero: str
    name: str
    time_played: int  # seconds
    win_rate: Optional[float]
    kda: Optional[float]


@dataclass
class PlayerData:
    battletag: str
    username: str
    avatar: Optional[str]
    rank_tank: Optional[str]
    rank_damage: Optional[str]
    rank_support: Optional[str]
    rank_open: Optional[str]
    games_played: Optional[int]
    games_won: Optional[int]
    games_lost: Optional[int]
    kda: Optional[float]
    win_rate: Optional[float]
    top_heroes: list[HeroStat] = field(default_factory=list)
    raw_summary: dict = field(default_factory=dict)
    raw_stats: dict = field(default_factory=dict)


def _battletag_to_url(battletag: str) -> str:
    return battletag.replace("#", "-")


def _rank_str(role_data: Optional[dict]) -> Optional[str]:
    if not role_data:
        return None
    division = role_data.get("division", "")
    tier = role_data.get("tier", "")
    if division:
        return f"{division.capitalize()} {tier}" if tier else division.capitalize()
    return None


def _parse_summary(battletag: str, data: dict) -> dict:
    comp = data.get("competitive") or {}
    pc = comp.get("pc") or {}
    return {
        "username": data.get("username", battletag.split("#")[0]),
        "avatar": data.get("avatar"),
        "rank_tank": _rank_str(pc.get("tank")),
        "rank_damage": _rank_str(pc.get("damage")),
        "rank_support": _rank_str(pc.get("support")),
        "rank_open": _rank_str(pc.get("open_queue")),
    }


def _parse_stats(data: dict) -> dict:
    """
    Extract games_played, games_won, kda, win_rate, top_heroes from the
    /stats/summary response. The API returns general aggregate stats and
    a list of hero-specific summaries.
    """
    general = data.get("general") or {}
    heroes_raw = data.get("heroes") or []

    games_played = general.get("games_played")
    games_won = general.get("games_won")
    games_lost = general.get("games_lost")
    win_rate = general.get("winrate")
    kda = general.get("kda")

    # Compute win_rate ourselves if not provided directly
    if win_rate is None and games_played and games_played > 0 and games_won is not None:
        win_rate = games_won / games_played

    top_heroes = []
    for h in sorted(heroes_raw, key=lambda x: x.get("time_played", 0), reverse=True)[:5]:
        top_heroes.append({
            "hero": h.get("key", ""),
            "name": h.get("name", h.get("key", "").capitalize()),
            "time_played": h.get("time_played", 0),
            "win_rate": h.get("winrate"),
            "kda": h.get("kda"),
        })

    return {
        "games_played": games_played,
        "games_won": games_won,
        "games_lost": games_lost,
        "win_rate": win_rate,
        "kda": kda,
        "top_heroes": top_heroes,
    }


async def fetch_player(battletag: str) -> PlayerData:
    url_tag = _battletag_to_url(battletag)
    now = time.time()

    # Serve from cache if fresh
    if battletag in _cache:
        cached_at, cached_data = _cache[battletag]
        if now - cached_at < CACHE_TTL:
            return cached_data  # type: ignore[return-value]

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as client:
        summary_resp = await client.get(f"/players/{url_tag}/summary")
        if summary_resp.status_code == 404:
            raise PlayerNotFoundError(f"Player '{battletag}' not found")
        if summary_resp.status_code == 403:
            raise ProfilePrivateError(f"Profile for '{battletag}' is private")
        if summary_resp.status_code != 200:
            raise OverFastError(f"OverFast API error {summary_resp.status_code} for {battletag}")

        summary_data = summary_resp.json()

        stats_resp = await client.get(f"/players/{url_tag}/stats/summary")
        if stats_resp.status_code == 403:
            raise ProfilePrivateError(f"Profile for '{battletag}' is private")
        stats_data = stats_resp.json() if stats_resp.status_code == 200 else {}

    parsed_summary = _parse_summary(battletag, summary_data)
    parsed_stats = _parse_stats(stats_data)

    result = PlayerData(
        battletag=battletag,
        username=parsed_summary["username"],
        avatar=parsed_summary["avatar"],
        rank_tank=parsed_summary["rank_tank"],
        rank_damage=parsed_summary["rank_damage"],
        rank_support=parsed_summary["rank_support"],
        rank_open=parsed_summary["rank_open"],
        games_played=parsed_stats["games_played"],
        games_won=parsed_stats["games_won"],
        games_lost=parsed_stats["games_lost"],
        kda=parsed_stats["kda"],
        win_rate=parsed_stats["win_rate"],
        top_heroes=[HeroStat(**h) for h in parsed_stats["top_heroes"]],
        raw_summary=summary_data,
        raw_stats=stats_data,
    )

    _cache[battletag] = (now, result)
    return result


def invalidate_cache(battletag: str) -> None:
    _cache.pop(battletag, None)

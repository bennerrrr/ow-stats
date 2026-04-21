import asyncio
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
    stats_by_gamemode: dict = field(default_factory=dict)
    raw_summary: dict = field(default_factory=dict)
    raw_stats: dict = field(default_factory=dict)


HERO_ROLES: dict[str, str] = {
    # Tank
    "dva": "tank", "doomfist": "tank", "junker-queen": "tank", "mauga": "tank",
    "orisa": "tank", "ramattra": "tank", "reinhardt": "tank", "roadhog": "tank",
    "sigma": "tank", "winston": "tank", "wrecking-ball": "tank", "zarya": "tank",
    "hazard": "tank",
    # Damage
    "ashe": "damage", "bastion": "damage", "cassidy": "damage", "echo": "damage",
    "genji": "damage", "hanzo": "damage", "junkrat": "damage", "mei": "damage",
    "pharah": "damage", "reaper": "damage", "sojourn": "damage", "soldier-76": "damage",
    "sombra": "damage", "symmetra": "damage", "torbjorn": "damage", "tracer": "damage",
    "widowmaker": "damage", "venture": "damage", "freja": "damage",
    # Support
    "ana": "support", "baptiste": "support", "brigitte": "support", "illari": "support",
    "kiriko": "support", "lifeweaver": "support", "lucio": "support", "mercy": "support",
    "moira": "support", "zenyatta": "support", "juno": "support",
}


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
        "rank_open": _rank_str(pc.get("open")),
    }


def _parse_stats(data: dict) -> dict:
    general = data.get("general") or {}
    heroes_raw = data.get("heroes") or {}

    games_played = general.get("games_played")
    games_won = general.get("games_won")
    games_lost = general.get("games_lost")
    kda = general.get("kda")

    # API returns winrate as 0-100; normalize to 0-1 for storage
    raw_winrate = general.get("winrate")
    if raw_winrate is not None:
        win_rate = raw_winrate / 100.0
    elif games_played and games_played > 0 and games_won is not None:
        win_rate = games_won / games_played
    else:
        win_rate = None

    # heroes is now a dict {hero_name: {time_played, winrate, kda, ...}}
    hero_items = [
        {"hero": name, **stats}
        for name, stats in heroes_raw.items()
    ] if isinstance(heroes_raw, dict) else heroes_raw

    top_heroes = []
    for h in sorted(hero_items, key=lambda x: x.get("time_played") or 0, reverse=True)[:15]:
        raw_hero_winrate = h.get("winrate")
        top_heroes.append({
            "hero": h.get("hero", ""),
            "name": h.get("hero", "").replace("-", " ").title(),
            "time_played": h.get("time_played") or 0,
            "win_rate": raw_hero_winrate / 100.0 if raw_hero_winrate is not None else None,
            "kda": h.get("kda"),
            "damage_per_10_min": h.get("damage_per_10_min"),
            "healing_per_10_min": h.get("healing_per_10_min"),
            "eliminations_per_10_min": h.get("eliminations_per_10_min"),
        })

    return {
        "games_played": games_played,
        "games_won": games_won,
        "games_lost": games_lost,
        "win_rate": win_rate,
        "kda": kda,
        "top_heroes": top_heroes,
        "damage_per_10_min": general.get("damage_per_10_min"),
        "healing_per_10_min": general.get("healing_per_10_min"),
        "eliminations_per_10_min": general.get("eliminations_per_10_min"),
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
        summary_resp, stats_resp, comp_resp, qp_resp = await asyncio.gather(
            client.get(f"/players/{url_tag}/summary"),
            client.get(f"/players/{url_tag}/stats/summary"),
            client.get(f"/players/{url_tag}/stats/summary", params={"gamemode": "competitive"}),
            client.get(f"/players/{url_tag}/stats/summary", params={"gamemode": "quickplay"}),
        )

    if summary_resp.status_code == 404:
        raise PlayerNotFoundError(f"Player '{battletag}' not found")
    if summary_resp.status_code == 403:
        raise ProfilePrivateError(f"Profile for '{battletag}' is private")
    if summary_resp.status_code != 200:
        raise OverFastError(f"OverFast API error {summary_resp.status_code} for {battletag}")

    summary_data = summary_resp.json()
    stats_data = stats_resp.json() if stats_resp.status_code == 200 else {}
    comp_data = comp_resp.json() if comp_resp.status_code == 200 else {}
    qp_data = qp_resp.json() if qp_resp.status_code == 200 else {}

    parsed_summary = _parse_summary(battletag, summary_data)
    parsed_stats = _parse_stats(stats_data)

    parsed_comp = _parse_stats(comp_data)
    parsed_qp = _parse_stats(qp_data)

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
        stats_by_gamemode={
            "competitive": parsed_comp,
            "quickplay": parsed_qp,
        },
        raw_summary=summary_data,
        raw_stats=stats_data,
    )

    _cache[battletag] = (now, result)
    return result


def invalidate_cache(battletag: str) -> None:
    _cache.pop(battletag, None)

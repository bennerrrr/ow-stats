import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _slog(value: str) -> str:
    return str(value).replace("\n", " ").replace("\r", " ")

STEAM_API_BASE = "https://api.steampowered.com"
HLL_APP_ID = 686810

_cache: dict[str, tuple[float, "HLLPlayerData"]] = {}
CACHE_TTL = 1800  # 30 minutes — match the poll interval

# Maps raw Steam stat names to clean display names
_ROLE_XP_FIELDS: dict[str, str] = {
    "rolexp_rifleman":        "Rifleman",
    "rolexp_assault":         "Assault",
    "rolexp_autorifleman":    "Auto Rifleman",
    "rolexp_medic":           "Medic",
    "rolexp_Spotter":         "Spotter",
    "rolexp_Support":         "Support",
    "rolexp_HeavyMachineGunner": "HMG",
    "rolexp_AntiTank":        "Anti-Tank",
    "rolexp_Engineer":        "Engineer",
    "rolexp_Officer":         "Officer",
    "rolexp_Sniper":          "Sniper",
    "rolexp_Crewman":         "Crewman",
    "rolexp_TankCommander":   "Tank Commander",
    "rolexp_ArmyCommander":   "Commander",
    "rolexp_spacommander":    "SPA Commander",
    "rolexp_spagunner":       "SPA Gunner",
    "rolexp_ArtilleryObserver": "Artillery Observer",
    "rolexp_artilleryengineer": "Artillery Engineer",
    "rolexp_artillerysupport":  "Artillery Support",
}

_COMBAT_STAT_FIELDS = {
    "ACHSTAT_EnemyKills":    "kills",
    "ACHSTAT_Headshots":     "headshots",
    "ACHSTAT_TankKills":     "tank_kills",
    "ACHSTAT_VehicleKills":  "vehicle_kills",
    "ACHSTAT_ArtilleryKills":"artillery_kills",
    "ACHSTAT_KnifeKills":    "knife_kills",
    "ACHSTAT_SpadeKills":    "spade_kills",
    "ACHSTAT_SectorCaps":    "sector_caps",
    "ACHSTAT_AmmoDrops":     "ammo_drops",
    "ACHSTAT_SupplyDrops":   "supply_drops",
    "ACHSTAT_CommendRecv":   "commendations",
    "player_xp":             "total_xp",
}


class PlayerNotFoundError(Exception):
    pass


class ProfilePrivateError(Exception):
    pass


class HLLClientError(Exception):
    pass


@dataclass
class HLLPlayerData:
    steam_id: str
    display_name: str
    avatar: Optional[str]
    playtime_forever: Optional[int]    # total minutes played (all time)
    playtime_2weeks: Optional[int]     # minutes played in last 2 weeks
    kills: Optional[int] = None
    headshots: Optional[int] = None
    tank_kills: Optional[int] = None
    vehicle_kills: Optional[int] = None
    artillery_kills: Optional[int] = None
    knife_kills: Optional[int] = None
    spade_kills: Optional[int] = None
    sector_caps: Optional[int] = None
    ammo_drops: Optional[int] = None
    supply_drops: Optional[int] = None
    commendations: Optional[int] = None
    total_xp: Optional[int] = None
    top_role: Optional[str] = None
    role_xp: dict = field(default_factory=dict)  # {display_name: xp_value}


async def _get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with up to 3 attempts and exponential backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = await client.get(url, **kwargs)
            if resp.status_code < 500:
                return resp
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_exc = exc
        if attempt < 2:
            await asyncio.sleep(2 ** attempt)
    if last_exc:
        raise last_exc
    return resp  # type: ignore[return-value]


async def fetch_player(steam_id: str, api_key: str) -> HLLPlayerData:
    """
    Fetch HLL player data via Steam Web API.
    Requires the player's Steam profile to have 'Game details' set to public.
    """
    now = time.time()
    if steam_id in _cache:
        cached_at, cached_data = _cache[steam_id]
        if now - cached_at < CACHE_TTL:
            return cached_data

    async with httpx.AsyncClient(base_url=STEAM_API_BASE, timeout=15.0) as client:
        summary_resp, games_resp, stats_resp = await asyncio.gather(
            _get_with_retry(client, "/ISteamUser/GetPlayerSummaries/v0002/", params={
                "key": api_key,
                "steamids": steam_id,
            }),
            _get_with_retry(client, "/IPlayerService/GetOwnedGames/v0001/", params={
                "key": api_key,
                "steamid": steam_id,
                "include_appinfo": "false",
                "include_played_free_games": "true",
                "appids_filter[0]": str(HLL_APP_ID),
            }),
            _get_with_retry(client, "/ISteamUserStats/GetUserStatsForGame/v0002/", params={
                "key": api_key,
                "steamid": steam_id,
                "appid": str(HLL_APP_ID),
            }),
        )

    if summary_resp.status_code != 200:
        raise HLLClientError(f"Steam API error ({summary_resp.status_code})")

    players = summary_resp.json().get("response", {}).get("players", [])
    if not players:
        raise PlayerNotFoundError(f"Steam ID {steam_id!r} not found")

    profile = players[0]
    if profile.get("communityvisibilitystate", 1) < 3:
        raise ProfilePrivateError(f"Steam profile for {steam_id} is private")

    display_name = profile.get("personaname", steam_id)
    avatar = profile.get("avatarmedium") or profile.get("avatar")

    # Playtime from owned games
    playtime_forever = None
    playtime_2weeks = None
    if games_resp.status_code == 200:
        for game in games_resp.json().get("response", {}).get("games", []):
            if game.get("appid") == HLL_APP_ID:
                playtime_forever = game.get("playtime_forever")
                playtime_2weeks = game.get("playtime_2weeks")
                break

    # Combat and role stats
    combat: dict[str, int] = {}
    role_xp: dict[str, int] = {}

    if stats_resp.status_code == 200:
        raw_stats = {
            s["name"]: s["value"]
            for s in stats_resp.json().get("playerstats", {}).get("stats", [])
        }
        logger.info(
            "HLL stats for %s: %d stat fields returned. Combat sample — kills=%s xp=%s",
            _slog(steam_id),
            len(raw_stats),
            raw_stats.get("ACHSTAT_EnemyKills"),
            raw_stats.get("player_xp"),
        )
        for steam_key, field_name in _COMBAT_STAT_FIELDS.items():
            if steam_key in raw_stats:
                combat[field_name] = raw_stats[steam_key]
        for steam_key, role_name in _ROLE_XP_FIELDS.items():
            if steam_key in raw_stats and raw_stats[steam_key] > 0:
                role_xp[role_name] = raw_stats[steam_key]
    elif stats_resp.status_code == 403:
        logger.warning("HLL stats for %s are private (403) — game details not public", _slog(steam_id))
    else:
        logger.warning("HLL stats request failed for %s: HTTP %d", _slog(steam_id), stats_resp.status_code)

    top_role = max(role_xp, key=role_xp.get) if role_xp else None

    result = HLLPlayerData(
        steam_id=steam_id,
        display_name=display_name,
        avatar=avatar,
        playtime_forever=playtime_forever,
        playtime_2weeks=playtime_2weeks,
        top_role=top_role,
        role_xp=role_xp,
        **{k: combat.get(k) for k in [
            "kills", "headshots", "tank_kills", "vehicle_kills",
            "artillery_kills", "knife_kills", "spade_kills",
            "sector_caps", "ammo_drops", "supply_drops",
            "commendations", "total_xp",
        ]},
    )
    _cache[steam_id] = (now, result)
    return result


def invalidate_cache(steam_id: str) -> None:
    _cache.pop(steam_id, None)

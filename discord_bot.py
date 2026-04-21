import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from database import AsyncSessionLocal
from models import DiscordChannel, Player, StatSnapshot
from ow_client import OverFastError, PlayerNotFoundError, ProfilePrivateError, fetch_player

logger = logging.getLogger(__name__)

OW_COLOR = 0xF99E1A
WIN_COLOR = 0x57F287
LOSS_COLOR = 0xED4245
TIE_COLOR = 0xFEE75C

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


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_stats_embed(player: Player, snapshot: StatSnapshot) -> discord.Embed:
    name = player.display_name or player.battletag
    embed = discord.Embed(title=name, color=OW_COLOR)
    embed.set_author(name=player.battletag)
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

    if snapshot.top_heroes:
        hero_lines = []
        for h in snapshot.top_heroes[:3]:
            time_str = _fmt_time(h.get("time_played", 0))
            wr = h.get("win_rate")
            wr_str = f" · {wr:.0%} WR" if wr is not None else ""
            hero_lines.append(f"**{h.get('name', h.get('hero', '?'))}:** {time_str}{wr_str}")
        embed.add_field(name="Top Heroes", value="\n".join(hero_lines), inline=False)

    embed.set_footer(text=f"Last updated · {snapshot.fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


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

    embed = discord.Embed(title=f"🎮 Game Report — {player_name}", color=color)
    embed.set_author(name=battletag)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    # Session result
    if games_delta == 1:
        session_str = "✅ **Win!**" if wins_delta == 1 else "❌ **Loss**"
    else:
        session_str = f"+{wins_delta}W / +{losses_delta}L over {games_delta} games"
    embed.add_field(name="Session Result", value=session_str, inline=False)

    # Rank changes
    rank_changes = []
    for label, key in [("Tank", "rank_tank"), ("Damage", "rank_damage"),
                        ("Support", "rank_support"), ("Open Queue", "rank_open")]:
        p, n = prev[key], new[key]
        if p != n:
            rank_changes.append(f"**{label}:** {_rank_display(p) if p else 'Unranked'} → {_rank_display(n) if n else 'Unranked'}")
    if rank_changes:
        embed.add_field(name="Rank Changes", value="\n".join(rank_changes), inline=False)

    # Current stats
    stat_lines = []
    if new["win_rate"] is not None:
        stat_lines.append(f"**Win Rate:** {new['win_rate']:.1%}")
    if new["kda"] is not None:
        stat_lines.append(f"**KDA:** {new['kda']:.2f}")
    if new["games_played"] is not None:
        stat_lines.append(f"**Total Games:** {new['games_played']}")
    if stat_lines:
        embed.add_field(name="Current Stats", value="\n".join(stat_lines), inline=True)

    fetched_at: datetime = new["fetched_at"]
    embed.set_footer(text=f"Detected · {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    return embed


# ---------------------------------------------------------------------------
# Game report dispatch (called by scheduler)
# ---------------------------------------------------------------------------

async def send_game_report(
    player_name: str,
    battletag: str,
    avatar_url: str | None,
    prev: dict,
    new: dict,
) -> None:
    if not bot.is_ready():
        return

    embed = build_game_report_embed(player_name, battletag, avatar_url, prev, new)

    async with AsyncSessionLocal() as session:
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
                logger.error("Failed to send game report to %s: %s", ch.channel_id, e)


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


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="add_player", description="Start tracking an Overwatch 2 player")
@app_commands.describe(battletag="Player battletag, e.g. Username#1234")
async def cmd_add_player(interaction: discord.Interaction, battletag: str):
    await interaction.response.defer()
    battletag = battletag.strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == battletag))
        if result.scalar_one_or_none():
            await interaction.followup.send(f"**{battletag}** is already being tracked.", ephemeral=True)
            return

    try:
        data = await fetch_player(battletag)
    except PlayerNotFoundError:
        await interaction.followup.send(
            f"Player `{battletag}` not found. Check the format — should be `Username#1234`.",
            ephemeral=True,
        )
        return
    except ProfilePrivateError:
        await interaction.followup.send(f"**{battletag}**'s profile is private.", ephemeral=True)
        return
    except OverFastError as e:
        await interaction.followup.send(f"API error fetching `{battletag}`: {e}", ephemeral=True)
        return

    async with AsyncSessionLocal() as session:
        player = Player(battletag=battletag, display_name=data.username, avatar_url=data.avatar)
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
                 "win_rate": h.win_rate, "kda": h.kda}
                for h in data.top_heroes
            ],
            stats_by_gamemode=data.stats_by_gamemode,
            raw_summary=data.raw_summary,
            raw_stats=data.raw_stats,
        )
        session.add(snapshot)
        await session.commit()

        embed = build_stats_embed(player, snapshot)
        await interaction.followup.send(f"Now tracking **{battletag}**!", embed=embed)


@bot.tree.command(name="remove_player", description="Stop tracking an Overwatch 2 player")
@app_commands.describe(battletag="Player battletag to remove")
async def cmd_remove_player(interaction: discord.Interaction, battletag: str):
    battletag = battletag.strip()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == battletag))
        player = result.scalar_one_or_none()
        if not player:
            await interaction.response.send_message(
                f"**{battletag}** is not currently tracked.", ephemeral=True
            )
            return
        await session.delete(player)
        await session.commit()
    await interaction.response.send_message(f"Stopped tracking **{battletag}**.")


@bot.tree.command(name="stats", description="Show the latest stats panel for a tracked player")
@app_commands.describe(battletag="Player battletag")
async def cmd_stats(interaction: discord.Interaction, battletag: str):
    await interaction.response.defer()
    battletag = battletag.strip()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).where(Player.battletag == battletag))
        player = result.scalar_one_or_none()
        if not player:
            await interaction.followup.send(
                f"**{battletag}** is not tracked. Use `/add_player` first.", ephemeral=True
            )
            return

        snap_result = await session.execute(
            select(StatSnapshot)
            .where(StatSnapshot.player_id == player.id)
            .order_by(StatSnapshot.fetched_at.desc())
            .limit(1)
        )
        snapshot = snap_result.scalar_one_or_none()
        if not snapshot:
            await interaction.followup.send(
                f"No stats yet for **{battletag}** — check back after the next poll."
            )
            return

        embed = build_stats_embed(player, snapshot)
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="players", description="List all currently tracked players")
async def cmd_players(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Player).order_by(Player.added_at))
        players = result.scalars().all()

    if not players:
        await interaction.response.send_message(
            "No players tracked yet. Use `/add_player` to get started."
        )
        return

    lines = [f"• **{p.display_name or p.battletag}** (`{p.battletag}`)" for p in players]
    embed = discord.Embed(
        title=f"Tracked Players ({len(players)})",
        description="\n".join(lines),
        color=OW_COLOR,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="set_channel",
    description="Register this channel to receive game notifications",
)
async def cmd_set_channel(interaction: discord.Interaction):
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

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiscordChannel).where(DiscordChannel.channel_id == str(interaction.channel_id))
        )
        if result.scalar_one_or_none():
            await interaction.response.send_message(
                "This channel is already registered for notifications.", ephemeral=True
            )
            return

        session.add(DiscordChannel(
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
            channel_name=channel_name,
        ))
        await session.commit()

    await interaction.response.send_message(
        f"✅ **#{channel_name}** will now receive game reports when tracked players finish games!"
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

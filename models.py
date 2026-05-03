from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Integer, String, Float, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class DiscordChannel(Base):
    __tablename__ = "discord_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False)
    channel_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    channel_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # NULL = receives all games; "overwatch" / "hell_let_loose" = game-specific
    game: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battletag: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    game: Mapped[str] = mapped_column(String, nullable=False, default="overwatch", server_default="overwatch")
    display_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    snapshots: Mapped[list["StatSnapshot"]] = relationship(
        "StatSnapshot", back_populates="player", cascade="all, delete-orphan",
        order_by="StatSnapshot.fetched_at.desc()"
    )


class StatSnapshot(Base):
    __tablename__ = "stat_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey("players.id"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    # Competitive ranks (nullable — player may not be ranked)
    rank_tank: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rank_damage: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rank_support: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rank_open: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Career aggregate stats
    games_played: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    games_won: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    games_lost: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    kda: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Top heroes JSON: [{"hero": "ana", "name": "Ana", "time_played": 3600, "winrate": 0.55, "kda": 3.1}]
    top_heroes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Per-gamemode breakdown: {"competitive": {games_played, win_rate, kda, top_heroes, ...}, "quickplay": {...}}
    stats_by_gamemode: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Full raw API responses for forward-compatibility
    raw_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    raw_stats: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Game-specific data (used by HLL: bm_player_id, sessions, kills, deaths, k_d_ratio, etc.)
    game_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    player: Mapped["Player"] = relationship("Player", back_populates="snapshots")

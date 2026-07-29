"""SQLAlchemy models for Discord-Minecraft chat sync."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class DiscordEvent(Base):
    """Model for discord_events table - stores Minecraft events to be sent to Discord."""

    __tablename__ = "discord_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    player_name: Mapped[str] = mapped_column(String(64), nullable=False)
    player_uuid: Mapped[str] = mapped_column(String(36), nullable=False)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_unprocessed", "processed_at", "created_at"),)

    def __repr__(self) -> str:
        return f"<DiscordEvent(id={self.id}, type={self.event_type}, player={self.player_name})>"

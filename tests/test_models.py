"""Tests for SQLAlchemy models."""

from datetime import datetime

import pytest

from models import Base, DiscordEvent


class TestDiscordEventModel:
    """Tests for DiscordEvent model."""

    def test_model_has_correct_tablename(self):
        """Test that model has correct table name."""
        assert DiscordEvent.__tablename__ == "discord_events"

    def test_model_inherits_from_base(self):
        """Test that model inherits from Base."""
        assert issubclass(DiscordEvent, Base)

    def test_model_has_required_columns(self):
        """Test that model has all required columns."""
        columns = DiscordEvent.__table__.columns
        column_names = [c.name for c in columns]

        assert "id" in column_names
        assert "event_type" in column_names
        assert "player_name" in column_names
        assert "player_uuid" in column_names
        assert "message" in column_names
        assert "created_at" in column_names
        assert "processed_at" in column_names

    def test_model_id_is_primary_key(self):
        """Test that id is the primary key."""
        id_column = DiscordEvent.__table__.columns["id"]
        assert id_column.primary_key is True

    def test_model_id_is_autoincrement(self):
        """Test that id has autoincrement."""
        id_column = DiscordEvent.__table__.columns["id"]
        assert id_column.autoincrement is True

    def test_model_event_type_not_nullable(self):
        """Test that event_type is not nullable."""
        column = DiscordEvent.__table__.columns["event_type"]
        assert column.nullable is False

    def test_model_player_name_not_nullable(self):
        """Test that player_name is not nullable."""
        column = DiscordEvent.__table__.columns["player_name"]
        assert column.nullable is False

    def test_model_player_uuid_not_nullable(self):
        """Test that player_uuid is not nullable."""
        column = DiscordEvent.__table__.columns["player_uuid"]
        assert column.nullable is False

    def test_model_message_nullable(self):
        """Test that message is nullable."""
        column = DiscordEvent.__table__.columns["message"]
        assert column.nullable is True

    def test_model_processed_at_nullable(self):
        """Test that processed_at is nullable."""
        column = DiscordEvent.__table__.columns["processed_at"]
        assert column.nullable is True

    def test_model_has_index(self):
        """Test that model has the idx_unprocessed index."""
        indexes = DiscordEvent.__table__.indexes
        index_names = [idx.name for idx in indexes]
        assert "idx_unprocessed" in index_names

    def test_model_repr(self):
        """Test the string representation of the model."""
        event = DiscordEvent()
        event.id = 123
        event.event_type = "chat"
        event.player_name = "Steve"

        repr_str = repr(event)

        assert "DiscordEvent" in repr_str
        assert "id=123" in repr_str
        assert "type=chat" in repr_str
        assert "player=Steve" in repr_str


class TestBaseModel:
    """Tests for Base model class."""

    def test_base_is_declarative(self):
        """Test that Base is a declarative base."""
        from sqlalchemy.orm import DeclarativeBase
        assert issubclass(Base, DeclarativeBase)

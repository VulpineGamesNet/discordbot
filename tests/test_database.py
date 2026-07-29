"""Tests for database functionality."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import DatabaseConfig
from database import DatabaseManager
from models import DiscordEvent


@pytest.fixture
def db_config():
    """Create a test database configuration."""
    return DatabaseConfig(
        url="jdbc:mysql://test_user:test_pass@localhost:3306/test_minecraft",
    )


@pytest.fixture
def db_manager(db_config):
    """Create a DatabaseManager with mocked engine."""
    with patch("database.create_async_engine") as mock_engine:
        with patch("database.async_sessionmaker") as mock_session:
            manager = DatabaseManager(db_config)
            manager._engine = mock_engine.return_value
            manager._session_factory = mock_session.return_value
            return manager


class TestDatabaseManagerInit:
    """Tests for DatabaseManager initialization."""

    def test_init_creates_engine(self, db_config):
        """Test that initialization creates an async engine."""
        with patch("database.create_async_engine") as mock_engine:
            with patch("database.async_sessionmaker"):
                manager = DatabaseManager(db_config)

        mock_engine.assert_called_once()
        call_args = mock_engine.call_args[0][0]
        assert "mysql+asyncmy://test_user:test_pass@localhost:3306/test_minecraft" == call_args

    def test_init_not_initialized(self, db_config):
        """Test that manager starts as not initialized."""
        with patch("database.create_async_engine"):
            with patch("database.async_sessionmaker"):
                manager = DatabaseManager(db_config)

        assert manager.is_initialized is False


class TestDatabaseManagerInitialize:
    """Tests for DatabaseManager.initialize()."""

    @pytest.mark.asyncio
    async def test_initialize_success(self, db_manager):
        """Test successful database initialization."""
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock()
        mock_conn.run_sync = AsyncMock()

        db_manager._engine.begin.return_value = mock_conn

        result = await db_manager.initialize()

        assert result is True
        assert db_manager.is_initialized is True

    @pytest.mark.asyncio
    async def test_initialize_failure(self, db_manager):
        """Test database initialization failure."""
        db_manager._engine.begin.side_effect = Exception("Connection failed")

        result = await db_manager.initialize()

        assert result is False
        assert db_manager.is_initialized is False


class TestDatabaseManagerClose:
    """Tests for DatabaseManager.close()."""

    @pytest.mark.asyncio
    async def test_close_disposes_engine(self, db_manager):
        """Test that close disposes the engine."""
        db_manager._engine.dispose = AsyncMock()

        await db_manager.close()

        db_manager._engine.dispose.assert_called_once()


class TestDatabaseManagerGetUnprocessedEvents:
    """Tests for DatabaseManager.get_unprocessed_events()."""

    @pytest.mark.asyncio
    async def test_get_events_not_initialized(self, db_manager):
        """Test that returns empty list when not initialized."""
        db_manager._initialized = False

        result = await db_manager.get_unprocessed_events()

        assert result == []

    @pytest.mark.asyncio
    async def test_get_events_success(self, db_manager):
        """Test successful event retrieval."""
        db_manager._initialized = True

        mock_event = MagicMock(spec=DiscordEvent)
        mock_event.id = 1
        mock_event.event_type = "chat"
        mock_event.player_name = "Steve"
        mock_event.player_uuid = "abc-123"
        mock_event.message = "Hello"

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_event]

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        db_manager._session_factory.return_value = mock_session

        result = await db_manager.get_unprocessed_events()

        assert len(result) == 1
        assert result[0].player_name == "Steve"

    @pytest.mark.asyncio
    async def test_get_events_exception(self, db_manager):
        """Test that returns empty list on exception."""
        db_manager._initialized = True

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        db_manager._session_factory.return_value = mock_session

        result = await db_manager.get_unprocessed_events()

        assert result == []


class TestDatabaseManagerMarkEventsProcessed:
    """Tests for DatabaseManager.mark_events_processed()."""

    @pytest.mark.asyncio
    async def test_mark_not_initialized(self, db_manager):
        """Test that returns False when not initialized."""
        db_manager._initialized = False

        result = await db_manager.mark_events_processed([1, 2, 3])

        assert result is False

    @pytest.mark.asyncio
    async def test_mark_empty_list(self, db_manager):
        """Test that returns False for empty list."""
        db_manager._initialized = True

        result = await db_manager.mark_events_processed([])

        assert result is False

    @pytest.mark.asyncio
    async def test_mark_success(self, db_manager):
        """Test successful marking of events."""
        db_manager._initialized = True

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        db_manager._session_factory.return_value = mock_session

        result = await db_manager.mark_events_processed([1, 2, 3])

        assert result is True
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_mark_exception(self, db_manager):
        """Test that returns False on exception."""
        db_manager._initialized = True

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=Exception("DB error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        db_manager._session_factory.return_value = mock_session

        result = await db_manager.mark_events_processed([1, 2, 3])

        assert result is False

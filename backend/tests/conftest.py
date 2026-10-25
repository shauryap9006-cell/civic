"""Test fixtures and database session management."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session wrapped in a transaction that rolls back on exit."""
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as session:
        async with session.begin():
            yield session
            await session.rollback()
    await engine.dispose()

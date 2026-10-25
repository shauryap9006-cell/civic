"""Clock abstraction providing real and simulated time.

Required by rule #4 in AGENTS.md: Server code never calls datetime.now() directly.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class Clock:
    def real_now(self) -> datetime:
        """Always returns the real current UTC timestamp.

        Used strictly for request signature tolerance checks
        (never affected by simulated demo clock).
        """
        return datetime.now(UTC)

    async def get_offset_seconds(self, session: AsyncSession) -> int:
        """Retrieve simulated clock offset from app_state table."""
        stmt = text("SELECT value FROM app_state WHERE key = 'clock_offset_seconds'")
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return 0
        try:
            return int(row)
        except (ValueError, TypeError):
            return 0

    async def set_offset_seconds(self, session: AsyncSession, offset_seconds: int) -> None:
        """Update simulated clock offset in app_state table."""
        stmt = text(
            """
            INSERT INTO app_state (key, value)
            VALUES ('clock_offset_seconds', :val)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """
        )
        await session.execute(stmt, {"val": str(offset_seconds)})
        await session.commit()

    async def advance_offset_seconds(self, session: AsyncSession, advance_seconds: int) -> int:
        """Add seconds to the simulated clock offset and return new offset."""
        current_offset = await self.get_offset_seconds(session)
        new_offset = current_offset + advance_seconds
        await self.set_offset_seconds(session, new_offset)
        return new_offset

    async def now(self, session: AsyncSession) -> datetime:
        """Returns the logical server timestamp (real UTC + clock_offset_seconds).

        All state machine transitions, pass evaluations, and escalation logic must use this.
        """
        offset = await self.get_offset_seconds(session)
        return self.real_now() + timedelta(seconds=offset)


clock = Clock()

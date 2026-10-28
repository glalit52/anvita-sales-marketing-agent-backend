import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(
            s.database_url,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, transaction, connection) -> None:  # noqa: ANN001
    """Pin the tenant for Postgres RLS at the start of every transaction.

    The settings are transaction-local (`set_config(..., true)`), so they reset at every commit and a
    pooled connection never leaks one tenant's context into another request.
    """
    org_id = session.info.get("org_id")
    bypass = session.info.get("rls_bypass", False)
    connection.execute(
        text("select set_config('app.org_id', :org, true), set_config('app.rls_bypass', :bypass, true)"),
        {"org": str(org_id) if org_id else "", "bypass": "on" if bypass else "off"},
    )


async def set_tenant(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    """Scope the session to a tenant, both for the open transaction (if any) and every later one."""
    session.info["org_id"] = org_id
    if session.in_transaction():
        await session.execute(
            text("select set_config('app.org_id', :org, true)"), {"org": str(org_id) if org_id else ""}
        )


@asynccontextmanager
async def tenant_session(org_id: uuid.UUID | None = None, *, bypass_rls: bool = False) -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        session.info["org_id"] = org_id
        session.info["rls_bypass"] = bypass_rls
        yield session

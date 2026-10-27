import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import Event


def emit(
    db: AsyncSession,
    org_id: uuid.UUID | None,
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    delay_seconds: float = 0,
    dedupe_key: str | None = None,
) -> Event:
    """Queue an event in the caller's transaction (transactional outbox): it is delivered only if the
    surrounding change commits, and at least once after that."""
    event = Event(
        org_id=org_id,
        type=type,
        payload={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in (payload or {}).items()},
        dedupe_key=dedupe_key,
        available_at=utcnow() + timedelta(seconds=delay_seconds),
    )
    db.add(event)
    return event

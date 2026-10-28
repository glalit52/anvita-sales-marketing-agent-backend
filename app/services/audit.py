import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def audit(
    db: AsyncSession,
    org_id: uuid.UUID,
    action: str,
    *,
    actor_type: str = "user",
    actor_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            org_id=org_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
        )
    )

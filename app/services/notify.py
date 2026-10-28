import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


def notify(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    tab: str,
    icon: str,
    title: str,
    body: str = "",
    user_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    link: str | None = None,
    priority: bool = False,
) -> Notification:
    n = Notification(
        org_id=org_id,
        user_id=user_id,
        tab=tab,
        icon=icon,
        title=title,
        body=body,
        contact_id=contact_id,
        link=link,
        priority=priority,
    )
    db.add(n)
    return n

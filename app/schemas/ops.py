import uuid
from datetime import date, datetime

from app.schemas.common import Schema


class NotificationOut(Schema):
    id: uuid.UUID
    tab: str
    icon: str
    title: str
    body: str
    contact_id: uuid.UUID | None
    link: str | None
    priority: bool
    unread: bool
    created_at: datetime


class DailyPoint(Schema):
    date: date
    leads: int
    ai_handled: int
    booked: int
    revenue_minor: int


class DashboardOut(Schema):
    range_days: int
    leads: int
    conversations: int
    ai_handled: int
    ai_resolution_rate: float
    bookings: int
    revenue_minor: int
    open_tasks: int
    overdue_tasks: int
    escalations_open: int
    median_first_response_seconds: int | None
    trend: list[DailyPoint]


class MarkReadIn(Schema):
    ids: list[uuid.UUID] | None = None

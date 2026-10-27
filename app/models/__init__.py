"""Import every model so Alembic autogenerate and the RLS policy list see the full schema."""

from app.models.agent import (
    AgentConfig,
    AgentRun,
    AgentVersion,
    EscalationRule,
    KnowledgeChunk,
    KnowledgeSource,
    Playbook,
)
from app.models.crm import ConsentRecord, Contact, ContactNote, Deal, Task
from app.models.inbox import Channel, Conversation, Message
from app.models.ops import Event, Notification
from app.models.org import AuditLog, Invite, Location, Membership, Organization, RefreshToken, User
from app.models.payments import Payment
from app.models.scheduling import Appointment, Provider, Service

__all__ = [
    "AgentConfig",
    "AgentRun",
    "AgentVersion",
    "Appointment",
    "AuditLog",
    "Channel",
    "ConsentRecord",
    "Contact",
    "ContactNote",
    "Conversation",
    "Deal",
    "EscalationRule",
    "Event",
    "Invite",
    "KnowledgeChunk",
    "KnowledgeSource",
    "Location",
    "Membership",
    "Message",
    "Notification",
    "Organization",
    "Payment",
    "Playbook",
    "Provider",
    "RefreshToken",
    "Service",
    "Task",
    "User",
]

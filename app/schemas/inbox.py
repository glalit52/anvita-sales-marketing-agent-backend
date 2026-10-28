import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import Schema
from app.schemas.crm import ChannelType, ContactOut

AgentMode = Literal["off", "copilot", "autopilot"]


class ChannelIn(Schema):
    type: ChannelType
    name: str = Field(max_length=200)
    external_id: str | None = Field(default=None, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)
    agent_mode: AgentMode = "autopilot"


class ChannelUpdate(Schema):
    name: str | None = None
    status: Literal["connected", "disconnected", "error"] | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    agent_mode: AgentMode | None = None


class ChannelOut(Schema):
    id: uuid.UUID
    type: str
    name: str
    status: str
    external_id: str | None
    config: dict[str, Any]
    agent_mode: AgentMode
    created_at: datetime


class MessageOut(Schema):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    author_type: Literal["contact", "ai", "human", "system"]
    author_id: uuid.UUID | None
    author_name: str | None
    body: str
    is_note: bool
    is_draft: bool
    card: dict[str, Any] | None
    sources: list[str]
    attachments: list[dict[str, Any]]
    status: str
    created_at: datetime


class ConversationOut(Schema):
    id: uuid.UUID
    contact_id: uuid.UUID
    channel_id: uuid.UUID | None
    channel: str = Field(validation_alias="channel_type")
    status: Literal["open", "pending", "closed"]
    assignee_type: Literal["ai", "human"]
    assignee_id: uuid.UUID | None
    escalation_reason: str | None
    ai_paused_until: datetime | None
    unread: int = Field(validation_alias="unread_count")
    intent: str | None
    sentiment: str | None
    ai_summary: str | None
    preview: str | None = Field(validation_alias="last_message_preview")
    last_message_at: datetime | None
    created_at: datetime


class ConversationListItem(ConversationOut):
    contact: ContactOut


class ConversationDetail(ConversationListItem):
    messages: list[MessageOut]


class ConversationUpdate(Schema):
    status: Literal["open", "pending", "closed"] | None = None
    # Assign to a teammate (assignee_id) or hand back to the AI (assignee_type="ai").
    assignee_type: Literal["ai", "human"] | None = None
    assignee_id: uuid.UUID | None = None
    ai_paused_until: datetime | None = None
    # Mark the escalation handled without changing who owns the thread.
    resolve_escalation: bool = False


class SendMessageIn(Schema):
    body: str = Field(min_length=1, max_length=4096)
    is_note: bool = False
    idempotency_key: str | None = Field(default=None, max_length=200)


class StartConversationIn(Schema):
    contact_id: uuid.UUID
    channel_type: ChannelType
    body: str = Field(min_length=1, max_length=4096)


class SuggestedReply(Schema):
    body: str
    sources: list[str]


class InboxCounts(Schema):
    all: int
    mine: int
    ai: int
    escalated: int
    unread: int

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings
from app.models import Channel, Contact, Message

log = logging.getLogger(__name__)


class SendError(Exception):
    """Delivery failed. `retryable` tells the worker whether trying again can help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class Capabilities:
    templates: bool = False
    media: bool = False
    read_receipts: bool = False
    # Free-form replies are only allowed this many hours after the contact's last message (None = always).
    window_hours: int | None = None


class ChannelAdapter(Protocol):
    type: str
    capabilities: Capabilities

    async def send(self, channel: Channel | None, contact: Contact, message: Message) -> str | None:
        """Deliver an outbound message; returns the provider's message id (None if not applicable)."""
        ...


class WebchatAdapter:
    """The widget pulls messages from our API, so "sending" is just making the message visible."""

    type = "webchat"
    capabilities = Capabilities(media=True, read_receipts=True)

    async def send(self, channel: Channel | None, contact: Contact, message: Message) -> str | None:
        return None


class DevOutboxAdapter:
    """Stand-in for channels without a provider integration yet. Logs the send outside production and
    refuses in production, so nothing silently disappears."""

    capabilities = Capabilities()

    def __init__(self, type: str) -> None:
        self.type = type

    async def send(self, channel: Channel | None, contact: Contact, message: Message) -> str | None:
        if get_settings().is_production:
            raise SendError(f"No provider integration for channel '{self.type}'", retryable=False)
        log.info("dev outbox send", extra={"data": {"channel": self.type, "to": contact.phone or contact.email}})
        return f"dev-{uuid.uuid4().hex}"


class ProviderChannelAdapter:
    """SMS and email: delivered through whichever provider is configured (Twilio, SMTP, …; see
    app/services/providers). Channel credentials, keyed by the provider's env var names, override platform env."""

    def __init__(self, type: str, kind: str, capabilities: Capabilities) -> None:
        self.type = type
        self.kind = kind
        self.capabilities = capabilities

    async def send(self, channel: Channel | None, contact: Contact, message: Message) -> str | None:
        from app.core.errors import AppError
        from app.services import providers

        try:
            provider = providers.resolve(self.kind, overrides=(channel.credentials if channel else None) or None)
        except AppError as e:
            raise SendError(e.message, retryable=False) from e
        if self.kind == "sms":
            if not contact.phone:
                raise SendError("Contact has no phone number", retryable=False)
            return await provider.send(to=contact.phone, text=message.body)
        if not contact.email:
            raise SendError("Contact has no email address", retryable=False)
        config = channel.config if channel else {}
        return await provider.send(
            to=contact.email,
            subject=config.get("default_subject") or "Re: your enquiry",
            text=message.body,
            from_name=config.get("from_name") or (message.author_name or "Team"),
            reply_to=config.get("reply_to"),
            headers={"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if config.get("unsubscribe_url") else {},
        )

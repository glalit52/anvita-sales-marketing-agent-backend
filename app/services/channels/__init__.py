from app.services.channels.base import ChannelAdapter, DevOutboxAdapter, SendError, WebchatAdapter
from app.services.channels.whatsapp import WhatsAppAdapter

_ADAPTERS: dict[str, ChannelAdapter] = {"webchat": WebchatAdapter(), "whatsapp": WhatsAppAdapter()}


def adapter_for(channel_type: str) -> ChannelAdapter:
    return _ADAPTERS.get(channel_type) or DevOutboxAdapter(channel_type)


def register_adapter(adapter: ChannelAdapter) -> None:
    _ADAPTERS[adapter.type] = adapter


__all__ = ["ChannelAdapter", "SendError", "adapter_for", "register_adapter"]

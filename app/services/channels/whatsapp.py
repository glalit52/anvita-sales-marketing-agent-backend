import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.security import constant_time_eq, hmac_sha256_hex
from app.models import Channel, Contact, Message
from app.services.channels.base import Capabilities, SendError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class InboundWhatsApp:
    phone_number_id: str
    wa_id: str
    profile_name: str | None
    provider_msg_id: str
    body: str
    kind: str


@dataclass(slots=True)
class StatusUpdate:
    provider_msg_id: str
    status: str  # sent | delivered | read | failed
    error: str | None


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Meta signs webhook bodies with the app secret: X-Hub-Signature-256: sha256=<hex>."""
    secret = get_settings().whatsapp_app_secret
    if secret is None:
        return not get_settings().is_production
    if not header or not header.startswith("sha256="):
        return False
    return constant_time_eq(header.removeprefix("sha256="), hmac_sha256_hex(secret.get_secret_value(), raw_body))


def parse_webhook(payload: dict[str, Any]) -> tuple[list[InboundWhatsApp], list[StatusUpdate]]:
    inbound: list[InboundWhatsApp] = []
    statuses: list[StatusUpdate] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                kind = m.get("type", "text")
                if kind == "text":
                    body = m.get("text", {}).get("body", "")
                elif kind == "button":
                    body = m.get("button", {}).get("text", "")
                elif kind == "interactive":
                    inter = m.get("interactive", {})
                    body = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
                else:
                    body = f"[{kind} message]"
                inbound.append(
                    InboundWhatsApp(
                        phone_number_id=phone_number_id,
                        wa_id=m.get("from", ""),
                        profile_name=names.get(m.get("from")),
                        provider_msg_id=m.get("id", ""),
                        body=body,
                        kind=kind,
                    )
                )
            for s in value.get("statuses", []):
                err = (s.get("errors") or [{}])[0].get("title") if s.get("errors") else None
                statuses.append(StatusUpdate(provider_msg_id=s.get("id", ""), status=s.get("status", ""), error=err))
    return inbound, statuses


class WhatsAppAdapter:
    type = "whatsapp"
    capabilities = Capabilities(templates=True, media=True, read_receipts=True, window_hours=24)

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def send(self, channel: Channel | None, contact: Contact, message: Message) -> str | None:
        if channel is None or not channel.external_id:
            raise SendError("WhatsApp channel is not configured", retryable=False)
        token = channel.credentials.get("access_token")
        if not token:
            raise SendError("WhatsApp access token missing", retryable=False)
        to = contact.whatsapp_id or (contact.phone or "").lstrip("+")
        if not to:
            raise SendError("Contact has no WhatsApp number", retryable=False)
        url = f"{get_settings().whatsapp_graph_url}/{channel.external_id}/messages"
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": message.body}}
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise SendError(f"WhatsApp request failed: {e}") from e
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code >= 500 or resp.status_code == 429:
            raise SendError(f"WhatsApp API {resp.status_code}")
        if resp.status_code >= 400:
            raise SendError(f"WhatsApp API {resp.status_code}: {resp.text[:300]}", retryable=False)
        return resp.json().get("messages", [{}])[0].get("id")

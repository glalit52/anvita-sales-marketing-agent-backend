import hashlib
import hmac
import json

import httpx
from pydantic import SecretStr

from app.core.config import get_settings
from app.services import knowledge
from app.services.agent.llm import set_llm
from app.services.channels import register_adapter
from app.services.channels.base import Capabilities
from app.worker import drain
from tests.conftest import ScriptedLLM, text_turn


def _wa_payload(msg_id: str, body: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PNID-1"},
                            "contacts": [{"wa_id": "919822011223", "profile": {"name": "Ananya Rao"}}],
                            "messages": [
                                {"from": "919822011223", "id": msg_id, "type": "text", "text": {"body": body}}
                            ],
                        }
                    }
                ]
            }
        ]
    }


class RecordingWhatsApp:
    type = "whatsapp"
    capabilities = Capabilities(templates=True, window_hours=24)

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, channel, contact, message):  # noqa: ANN001, ANN201
        self.sent.append(message.body)
        return f"wamid.out.{len(self.sent)}"


async def test_whatsapp_webhook_inbound_reply_and_status(client, ws, monkeypatch):
    secret = "app-secret"
    monkeypatch.setattr(get_settings(), "whatsapp_app_secret", SecretStr(secret))
    monkeypatch.setattr(get_settings(), "whatsapp_verify_token", SecretStr("verify-me"))
    ch = await ws.post(
        "/channels",
        {
            "type": "whatsapp",
            "name": "Clinic WhatsApp",
            "externalId": "PNID-1",
            "credentials": {"access_token": "EAAG..."},
        },
    )
    assert ch.status_code == 201
    assert "credentials" not in ch.json()  # secrets never leave the API

    verify = await client.get(
        "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "1234"}
    )
    assert verify.text == "1234"

    recorder = RecordingWhatsApp()
    register_adapter(recorder)
    set_llm(ScriptedLLM([text_turn("Hi Ananya! Yes, we do whitening.")]))
    raw = json.dumps(_wa_payload("wamid.in.1", "Do you do whitening?")).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert (
        await client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": "sha256=bad"})
    ).status_code == 401
    for _ in range(2):  # Meta retries deliveries; the second is a no-op
        r = await client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig})
        assert r.status_code == 200
    await drain()

    assert recorder.sent == ["Hi Ananya! Yes, we do whitening."]
    convs = (await ws.get("/conversations", params={"channel": "whatsapp"})).json()
    assert convs["total"] == 1
    conv = (await ws.get(f"/conversations/{convs['items'][0]['id']}")).json()
    assert conv["contact"]["phone"] == "+919822011223"
    assert [m["status"] for m in conv["messages"]] == ["received", "sent"]

    status = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.out.1", "status": "read"}]}}]}]}
    raw = json.dumps(status).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    await client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig})
    conv = (await ws.get(f"/conversations/{conv['id']}")).json()
    assert conv["messages"][-1]["status"] == "read"
    register_adapter(__import__("app.services.channels.whatsapp", fromlist=["x"]).WhatsAppAdapter())


async def test_whatsapp_reply_window_enforced_for_humans(ws):
    contact = (await ws.post("/contacts", {"name": "Old lead", "phone": "+919000011111"})).json()
    r = await ws.post("/conversations", {"contactId": contact["id"], "channelType": "whatsapp", "body": "Hi!"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "reply_window_closed"


def test_chunking_and_html_extraction():
    title, body, links = knowledge.html_to_text(
        "<html><head><title>Smile Dental</title><style>p{}</style></head><body><nav><a href='/pricing'>Pricing</a>"
        "</nav><p>Whitening from ₹8,000.</p><script>evil()</script></body></html>"
    )
    assert title == "Smile Dental" and "evil" not in body and "Whitening from ₹8,000." in body
    assert links == ["/pricing"]
    chunks = knowledge.chunk_text("\n".join(f"Paragraph {i} " + "x" * 300 for i in range(10)), size=1000, overlap=100)
    assert len(chunks) >= 3 and all(len(c) <= 1100 for c in chunks)


async def test_website_crawl_is_same_host_and_robots_aware(monkeypatch):
    pages = {
        "/robots.txt": "User-agent: *\nDisallow: /private",
        "/": "<title>Home</title><p>Welcome to Smile Dental.</p><a href='/pricing'>p</a><a href='/private/x'>x</a>"
        "<a href='https://other.com/'>o</a>",
        "/pricing": "<title>Pricing</title><p>Whitening ₹8,000–₹12,000.</p>",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "smile.example"
        if request.url.path not in pages:
            return httpx.Response(404)
        ctype = "text/plain" if request.url.path.endswith(".txt") else "text/html"
        return httpx.Response(200, text=pages[request.url.path], headers={"content-type": ctype})

    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(knowledge, "assert_public_url", public)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        result = await knowledge.crawl("https://smile.example/", client=c)
    assert [p.title for p in result] == ["Home", "Pricing"]


async def test_ssrf_guard_rejects_private_addresses():
    import pytest

    for url in ("http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"):
        with pytest.raises(knowledge.UnsafeURL):
            await knowledge.assert_public_url(url)


async def test_knowledge_api_search(ws):
    await ws.post("/agent/knowledge/faqs", {"question": "What are your hours?", "answer": "9 AM to 8 PM, Mon-Sat."})
    await ws.post(
        "/agent/knowledge",
        {
            "kind": "text",
            "title": "Price list 2026",
            "content": "Teeth whitening: ₹8,000-₹12,000.\nImplants: ₹45,000-₹60,000.",
        },
    )
    await drain()
    sources = (await ws.get("/agent/knowledge")).json()
    assert {s["status"] for s in sources} == {"synced"}
    hits = (await ws.get("/agent/knowledge/search", params={"q": "how much are implants"})).json()
    assert hits[0]["sourceTitle"] == "Price list 2026"

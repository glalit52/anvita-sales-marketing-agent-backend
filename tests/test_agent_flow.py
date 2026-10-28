from sqlalchemy import select

from app.db.session import tenant_session
from app.models import AgentRun, Appointment, Payment
from app.services.agent.llm import set_llm
from app.worker import drain
from tests.conftest import ScriptedLLM, last_tool_result, text_turn, tool_turn


async def _webchat(client, slug, name="Ananya"):
    s = await client.post(f"/public/webchat/{slug}/sessions", json={"name": name})
    assert s.status_code == 201, s.text
    return s.json(), {"Authorization": f"Bearer {s.json()['token']}"}


async def test_webchat_message_ai_books_and_sends_deposit_link(client, ws, clinic):
    await ws.post(
        "/agent/knowledge/faqs",
        {
            "question": "How much does teeth whitening cost?",
            "answer": "Laser whitening costs ₹8,000-₹12,000 and takes 60 minutes.",
        },
    )
    llm = ScriptedLLM(
        [
            tool_turn(
                "check_availability",
                {"service": "whitening consultation", "location": None, "date_from": None, "days": 7},
            ),
            tool_turn(
                "book_appointment",
                lambda m: {
                    "service": "Whitening consultation",
                    "location": "Koregaon Park",
                    "starts_at": last_tool_result(m)["slots"][0]["starts_at"],
                    "notes": None,
                },
                "toolu_2",
            ),
            tool_turn("create_payment_link", {"amount": 500, "purpose": "Consultation deposit"}, "toolu_3"),
            text_turn("You're booked ✅ Please pay the ₹500 deposit to confirm."),
        ]
    )
    set_llm(llm)
    session, wh = await _webchat(client, ws.org["slug"])
    sent = await client.post(
        "/public/webchat/messages", json={"body": "Can I book a whitening consult?", "clientId": "c-1"}, headers=wh
    )
    assert sent.status_code == 201
    # Widget retries are de-duplicated by clientId.
    await client.post(
        "/public/webchat/messages", json={"body": "Can I book a whitening consult?", "clientId": "c-1"}, headers=wh
    )

    assert await drain() >= 1

    msgs = (await client.get("/public/webchat/messages", headers=wh)).json()
    assert [m["author"] for m in msgs] == ["visitor", "ai"]
    reply = msgs[-1]
    assert reply["card"]["type"] == "booking"
    assert "/pay/" in reply["body"]  # payment URL is always included in the message

    # Prompt caching: the stable system block carries the cache breakpoint; tools are strict.
    convo_call = next(c for c in llm.calls if c["tools"])
    assert convo_call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] for t in convo_call["tools"])

    async with tenant_session(ws.org_uuid) as db:
        appt = (await db.scalars(select(Appointment))).one()
        assert appt.source == "ai" and appt.status == "confirmed"
        payment = (await db.scalars(select(Payment))).one()
        assert payment.amount_minor == 50000 and payment.created_by_type == "ai"
        run = (await db.scalars(select(AgentRun).where(AgentRun.kind == "reply"))).one()
        assert run.outcome == "replied"
        assert [c["tool"] for c in run.tool_calls] == ["check_availability", "book_appointment", "create_payment_link"]
        assert run.cost_usd > 0

    conv = (await ws.get(f"/conversations/{session['conversationId']}")).json()
    assert conv["intent"] == "Book appointment"
    assert conv["contact"]["stage"] == "booked"
    assert conv["channel"] == "webchat"


async def test_keyword_rule_escalates_without_calling_reply_model(client, ws):
    await ws.post(
        "/agent/escalation-rules",
        {
            "name": "Refund request",
            "kind": "keyword",
            "condition": {"keywords": ["refund"]},
            "action": "assign",
            "assignToId": ws.session["user"]["id"],
        },
    )
    llm = ScriptedLLM([])  # no conversation turns scripted: the reply model must not be called
    set_llm(llm)
    session, wh = await _webchat(client, ws.org["slug"], "Rohit")
    await client.post("/public/webchat/messages", json={"body": "I want a refund for my filling"}, headers=wh)
    await drain()

    conv = (await ws.get(f"/conversations/{session['conversationId']}")).json()
    assert conv["assigneeType"] == "human"
    assert conv["assigneeId"] == ws.session["user"]["id"]
    assert conv["escalationReason"] == "Refund request"
    assert [m["authorType"] for m in conv["messages"]] == ["contact", "ai", "system"]
    notes = (await ws.get("/notifications", params={"tab": "escalations"})).json()
    assert notes and notes[0]["priority"] is True
    assert (await ws.get("/conversations/counts")).json()["escalated"] == 1


async def test_human_reply_pauses_ai(client, ws):
    set_llm(ScriptedLLM([text_turn("Hi! How can I help?")]))
    session, wh = await _webchat(client, ws.org["slug"])
    await client.post("/public/webchat/messages", json={"body": "hello"}, headers=wh)
    await drain()
    cid = session["conversationId"]
    r = await ws.post(f"/conversations/{cid}/messages", {"body": "Priya here, I'll take this one."})
    assert r.status_code == 201

    await client.post("/public/webchat/messages", json={"body": "great, thanks"}, headers=wh)
    await drain()  # the scripted LLM has no turns left: a call would raise
    conv = (await ws.get(f"/conversations/{cid}")).json()
    assert conv["messages"][-1]["body"] == "great, thanks"
    assert conv["assigneeType"] == "human"

    # Handing back to the AI clears the pause.
    back = await ws.patch(f"/conversations/{cid}", {"assigneeType": "ai"})
    assert back.json()["assigneeType"] == "ai" and back.json()["aiPausedUntil"] is None


async def test_copilot_mode_drafts_then_human_approves(client, ws):
    await ws.patch("/agent/config", {"defaultMode": "copilot"})
    set_llm(ScriptedLLM([text_turn("We're open 9 to 6 every day.")]))
    session, wh = await _webchat(client, ws.org["slug"])
    await client.post("/public/webchat/messages", json={"body": "what are your hours"}, headers=wh)
    await drain()
    assert [m["author"] for m in (await client.get("/public/webchat/messages", headers=wh)).json()] == ["visitor"]

    conv = (await ws.get(f"/conversations/{session['conversationId']}")).json()
    draft = conv["messages"][-1]
    assert draft["isDraft"] and draft["authorType"] == "ai"
    sent = await ws.post(
        f"/conversations/{conv['id']}/drafts/{draft['id']}/send", {"body": "We're open 9 AM to 6 PM, every day!"}
    )
    assert sent.status_code == 200
    await drain()
    widget = (await client.get("/public/webchat/messages", headers=wh)).json()
    assert widget[-1]["body"] == "We're open 9 AM to 6 PM, every day!"


async def test_opt_out_keyword_records_consent(client, ws):
    set_llm(ScriptedLLM([]))
    contact = (await ws.post("/contacts", {"name": "Meera", "phone": "+919730055667"})).json()
    conv = await ws.post("/conversations", {"contactId": contact["id"], "channelType": "sms", "body": "Hi Meera!"})
    assert conv.status_code == 201
    from app.services.contacts import get_or_create_contact
    from app.services.inbox import record_inbound

    async with tenant_session(ws.org_uuid) as db:
        c, _ = await get_or_create_contact(db, ws.org_uuid, name=None, phone="+919730055667")
        conv_obj, _ = await record_inbound(db, ws.org_uuid, c, channel_type="sms", channel_id=None, body="STOP")
        conv_obj.assignee_type, conv_obj.ai_paused_until = "ai", None
        await db.commit()
    await drain()
    consents = (await ws.get(f"/contacts/{contact['id']}/consents")).json()
    assert consents == [
        {
            "channel": "sms",
            "purpose": "marketing",
            "status": "opted_out",
            "source": "Keyword 'STOP' on sms",
            "capturedAt": consents[0]["capturedAt"],
        }
    ]


async def test_offline_llm_answers_from_knowledge_base(client, ws):
    await ws.post(
        "/agent/knowledge/faqs",
        {"question": "Do you offer EMI for implants?", "answer": "Yes, 0% EMI over 6 months via Bajaj Finserv."},
    )
    session, wh = await _webchat(client, ws.org["slug"])
    await client.post("/public/webchat/messages", json={"body": "is EMI available for implants?"}, headers=wh)
    await drain()
    msgs = (await client.get("/public/webchat/messages", headers=wh)).json()
    assert "Bajaj Finserv" in msgs[-1]["body"]


async def test_sandbox_rolls_back_side_effects(ws, clinic):
    set_llm(
        ScriptedLLM(
            [
                tool_turn(
                    "check_availability",
                    {"service": "Whitening consultation", "location": "Koregaon Park", "date_from": None, "days": 7},
                ),
                tool_turn(
                    "book_appointment",
                    lambda m: {
                        "service": "Whitening consultation",
                        "location": "Koregaon Park",
                        "starts_at": last_tool_result(m)["slots"][0]["starts_at"],
                        "notes": None,
                    },
                    "toolu_2",
                ),
                text_turn("Booked!"),
            ]
        )
    )
    r = await ws.post("/agent/sandbox", {"messages": [{"role": "contact", "body": "Book me in"}]})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Booked!"
    assert [c["tool"] for c in r.json()["toolCalls"]] == ["check_availability", "book_appointment"]
    async with tenant_session(ws.org_uuid) as db:
        assert (await db.scalars(select(Appointment))).all() == []


async def test_inbound_message_not_blocked_while_agent_waits_on_model(client, ws):
    """A customer's follow-up must be storable (webhook ack) while the agent is mid tool loop."""
    from sqlalchemy import text

    from app.models import Contact
    from app.services.inbox import record_inbound

    session, wh = await _webchat(client, ws.org["slug"], "Zoya")
    await client.post("/public/webchat/messages", json={"body": "hi, I'm Zoya"}, headers=wh)
    contact_id = __import__("uuid").UUID(
        (await ws.get(f"/conversations/{session['conversationId']}")).json()["contactId"]
    )
    concurrent_ok: list[bool] = []

    async def store_followup(messages):  # runs *during* the second model call
        async with tenant_session(ws.org_uuid) as db:
            await db.execute(text("SET LOCAL lock_timeout = '1s'"))
            contact = await db.get(Contact, contact_id)
            await record_inbound(db, ws.org_uuid, contact, channel_type="webchat", channel_id=None, body="also EMI?")
            await db.commit()
        concurrent_ok.append(True)
        return text_turn("Nice to meet you, Zoya!")(messages)

    class ConcurrentLLM(ScriptedLLM):
        async def complete(self, **kw):
            if kw.get("json_schema") is None and len(self.turns) == 1:
                self.turns.pop(0)
                return await store_followup(kw["messages"])
            return await super().complete(**kw)

    set_llm(
        ConcurrentLLM(
            [
                tool_turn(
                    "update_contact",
                    {"name": "Zoya Mirza", "email": None, "city": "Pune", "language": None, "add_tags": "whitening"},
                ),
                text_turn("unused"),
            ]
        )
    )
    await drain(max_rounds=1)
    assert concurrent_ok == [True]
    conv = (await ws.get(f"/conversations/{session['conversationId']}")).json()
    assert conv["contact"]["name"] == "Zoya Mirza" and "whitening" in conv["contact"]["tags"]


async def test_suggest_reply_and_resolve_escalation(client, ws):
    set_llm(ScriptedLLM([text_turn("Hello! How can I help?"), text_turn("Sure, we're open 9 to 6.")]))
    session, wh = await _webchat(client, ws.org["slug"])
    await client.post("/public/webchat/messages", json={"body": "hi"}, headers=wh)
    await drain()
    cid = session["conversationId"]
    await client.post("/public/webchat/messages", json={"body": "what are your hours?"}, headers=wh)
    await ws.patch(f"/conversations/{cid}", {"assigneeType": "human", "assigneeId": ws.session["user"]["id"]})

    r = await ws.post(f"/conversations/{cid}/suggest-reply")
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "Sure, we're open 9 to 6."
    conv = (await ws.get(f"/conversations/{cid}")).json()
    assert conv["messages"][-1]["body"] == "what are your hours?"  # nothing was sent

    async with tenant_session(ws.org_uuid) as db:
        from app.models import Conversation

        c = await db.get(Conversation, __import__("uuid").UUID(cid))
        c.escalation_reason = "Negative sentiment"
        await db.commit()
    assert (await ws.patch(f"/conversations/{cid}", {"resolveEscalation": True})).json()["escalationReason"] is None

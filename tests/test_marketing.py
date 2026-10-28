import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.session import tenant_session
from app.models import Event, Message, ProspectRow
from app.services import knowledge
from app.services.knowledge import Page
from app.worker import drain


async def _contact(ws, name="Asha Patil", phone="+919822200001", **extra) -> str:
    r = await ws.post("/contacts", {"name": name, "phone": phone, **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_prospecting_end_to_end(ws):
    plan = (await ws.post("/prospecting/plan", {"prompt": "Dental clinics in Pune with a website, 4.0★+"})).json()
    assert plan["location"] == "Pune" and plan["minRating"] == 4.0 and plan["needsWebsite"] is True
    assert {s["id"] for s in (await ws.get("/prospecting/sources")).json()} == {"maps", "linkedin", "web"}
    before = (await ws.get("/prospecting/credits")).json()

    s = await ws.post("/prospecting/searches", {"name": "Pune dentists", "prompt": "x", "plan": {**plan, "limit": 20}})
    assert s.status_code == 202 and s.json()["status"] == "running"
    sid = s.json()["id"]
    await drain()
    done = (await ws.get(f"/prospecting/searches/{sid}")).json()
    assert done["status"] == "ready" and 0 < done["rowCount"] <= 20 and done["creditsUsed"] > 0
    rows = (await ws.get(f"/prospecting/searches/{sid}/rows")).json()
    assert all(r["rating"] is None or r["rating"] >= 4.0 for r in rows)
    assert rows == sorted(rows, key=lambda r: -r["fit"])
    after = (await ws.get("/prospecting/credits")).json()
    assert after["used"] - before["used"] == done["creditsUsed"]

    ids = [r["id"] for r in rows[:5]]
    stats = (await ws.post(f"/prospecting/searches/{sid}/enrich", {"rowIds": ids})).json()
    assert stats["credits"] == stats["emails"] + stats["phones"]

    col = await ws.post(f"/prospecting/searches/{sid}/columns", {"question": "Do they offer aligners?"})
    assert col.json()["aiColumns"] == [{"id": "c1", "question": "Do they offer aligners?"}]
    await drain()
    rows = (await ws.get(f"/prospecting/searches/{sid}/rows")).json()
    assert all(r["ai"]["c1"]["value"] in ("Yes", "No", "Unclear") for r in rows)

    seq = await ws.post(
        "/sequences",
        {"name": "Dentists", "steps": [{"type": "email", "day": 0, "title": "Intro", "body": "Hi {{first_name}}"}]},
    )
    reachable = [r["id"] for r in rows if r["phone"] or r["email"]][:3]
    added = (
        await ws.post(
            f"/prospecting/searches/{sid}/add-to-crm",
            {"rowIds": reachable, "tag": "pune-dentists", "sequenceId": seq.json()["id"]},
        )
    ).json()
    assert added["added"] == len(reachable) and added["enrolled"] == len(reachable)
    contacts = (await ws.get("/contacts", params={"tag": "pune-dentists"})).json()
    assert contacts["total"] == len(reachable) and contacts["items"][0]["stage"] == "prospect"

    csv = await ws.get(f"/prospecting/searches/{sid}/export.csv")
    assert csv.status_code == 200 and "Do they offer aligners?" in csv.text.splitlines()[0]


async def test_prospecting_without_credits(ws):
    async with tenant_session(ws.org_uuid) as db:
        from app.services import billing

        billing.charge(db, ws.org_uuid, "prospect", 10**6, "test: use everything")
        await db.commit()
    s = await ws.post("/prospecting/searches", {"name": "x", "prompt": "x", "plan": {"query": "gyms", "limit": 5}})
    await drain()
    failed = (await ws.get(f"/prospecting/searches/{s.json()['id']}")).json()
    assert failed["status"] == "failed" and "credits" in failed["error"]


async def test_workflow_tag_trigger_condition_and_actions(ws):
    wf = await ws.post(
        "/automations",
        {
            "name": "Hot lead",
            "trigger": {"type": "tag", "config": {"tag": "hot"}},
            "nodes": [
                {"id": "t", "kind": "trigger", "title": "Tag added"},
                {
                    "id": "n1",
                    "kind": "condition",
                    "type": "condition",
                    "title": "Is new?",
                    "config": {"field": "stage", "op": "eq", "value": "new"},
                    "no": [{"id": "n1a", "kind": "action", "type": "add_tag", "config": {"tag": "not-new"}}],
                },
                {"id": "n2", "kind": "action", "type": "add_tag", "config": {"tag": "vip"}},
                {"id": "n3", "kind": "action", "type": "send_sms", "config": {"body": "Hi {{first_name}}!"}},
                {"id": "n4", "kind": "action", "type": "create_task", "config": {"title": "Call {{first_name}}"}},
            ],
        },
    )
    assert wf.status_code == 201, wf.text
    wid = wf.json()["id"]
    assert (await ws.post(f"/automations/{wid}/toggle", params={"enabled": True})).json()["enabled"]
    bad = await ws.post(
        "/automations", {"name": "x", "trigger": {"type": "tag"}, "nodes": [{"kind": "action", "type": "nope"}]}
    )
    assert bad.status_code == 400

    a = await _contact(ws)
    b = await _contact(ws, "Bhavna", "+919822200002")
    await ws.patch(f"/contacts/{b}", {"stage": "customer"})
    await ws.patch(f"/contacts/{a}", {"tags": ["hot"]})
    await ws.patch(f"/contacts/{b}", {"tags": ["hot"]})
    await ws.patch(f"/contacts/{a}", {"tags": ["hot", "other"]})  # "hot" not newly added: no second run
    await drain()

    runs = {r["contactName"]: r for r in (await ws.get("/automations/runs")).json()}
    assert len(runs) == 2
    assert runs["Asha Patil"]["status"] == "success" and runs["Asha Patil"]["stepsDone"] == 4
    assert runs["Bhavna"]["status"] == "branched"
    assert set((await ws.get(f"/contacts/{a}")).json()["tags"]) == {"hot", "other", "vip"}
    assert "not-new" in (await ws.get(f"/contacts/{b}")).json()["tags"]
    async with tenant_session(ws.org_uuid) as db:
        bodies = (await db.scalars(select(Message.body).where(Message.direction == "outbound"))).all()
    assert bodies == ["Hi Asha!"]
    listed = (await ws.get("/automations")).json()[0]
    assert listed["runs"] == 2 and listed["successRate"] == 100


async def test_workflow_delay_template_and_api_event(ws):
    meta = (await ws.get("/automations/meta")).json()
    assert any(t["id"] == "no_show" for t in meta["templates"])
    wf = (await ws.post("/automations/from-template/payment_thanks")).json()
    assert wf["enabled"] is False and wf["trigger"]["type"] == "payment"

    custom = await ws.post(
        "/automations",
        {
            "name": "Webinar",
            "enabled": True,
            "trigger": {"type": "custom", "config": {"event": "webinar.registered"}},
            "nodes": [
                {"id": "n1", "kind": "delay", "type": "delay", "config": {"hours": 1}},
                {"id": "n2", "kind": "action", "type": "add_tag", "config": {"tag": "webinar"}},
            ],
        },
    )
    assert custom.status_code == 201
    r = await ws.post("/automations/events", {"event": "webinar.registered", "phone": "+919822200009", "name": "Kiran"})
    assert r.status_code == 202
    await drain()
    run = (await ws.get("/automations/runs")).json()[0]
    assert run["status"] == "waiting" and run["contactName"] == "Kiran"
    # Fast-forward the delayed resume.
    async with tenant_session(bypass_rls=True) as db:
        for e in (
            await db.scalars(select(Event).where(Event.type == "workflow.step", Event.processed_at.is_(None)))
        ).all():
            e.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    await drain()
    assert (await ws.get("/automations/runs")).json()[0]["status"] == "success"
    assert "webinar" in (await ws.get(f"/contacts/{r.json()['contactId']}")).json()["tags"]


async def test_marketing_ads_content_social(ws):
    camps = (await ws.get("/marketing/ads/campaigns", params={"days": 7})).json()
    assert len(camps) == 4 and all(len(c["days"]) == 7 for c in camps)
    recs = (await ws.post("/marketing/ads/recommendations/generate")).json()
    assert recs and recs[0]["status"] == "open"
    applied = (await ws.post(f"/marketing/ads/recommendations/{recs[0]['id']}/apply")).json()
    assert applied["status"] == "applied"
    assert len((await ws.post("/marketing/ads/copy", {"offer": "Teeth whitening"})).json()) >= 1

    item = (
        await ws.post("/marketing/content", {"title": "Teeth whitening cost", "keyword": "teeth whitening cost"})
    ).json()
    drafted = (await ws.post(f"/marketing/content/{item['id']}/draft")).json()
    assert drafted["stage"] == "Draft" and drafted["body"] and drafted["checks"][0]["pass"] is True
    appr = (await ws.post(f"/marketing/content/{item['id']}/submit")).json()
    assert appr["status"] == "pending"
    ok = await ws.post(f"/marketing/approvals/{appr['id']}/decide", {"decision": "approved"})
    assert ok.json()["status"] == "approved" and ok.json()["decidedByName"] == "Priya Sharma"
    assert (await ws.get("/marketing/content")).json()[0]["stage"] == "Published"
    ideas = (await ws.post("/marketing/content/ideas")).json()
    assert all(i["stage"] == "Idea" for i in ideas)

    assert len((await ws.post("/marketing/social/captions", {"topic": "Diwali hours"})).json()) == 3
    post = (await ws.post("/marketing/social", {"networks": ["instagram", "gbp"], "caption": "Open on Diwali"})).json()
    assert (await ws.post(f"/marketing/social/{post['id']}/schedule")).json()["status"] == "scheduled"
    await drain()
    published = (await ws.get("/marketing/social")).json()[0]
    assert published["status"] == "published" and set(published["externalIds"]) == {"instagram", "gbp"}


async def test_landing_page_approval_public_submit_and_form_workflow(client, ws):
    await ws.post(
        "/automations",
        {
            "name": "New lead",
            "enabled": True,
            "trigger": {"type": "form", "config": {}},
            "nodes": [{"id": "n1", "kind": "action", "type": "set_stage", "config": {"stage": "qualified"}}],
        },
    )
    page = await ws.post("/marketing/landing-pages", {"name": "Whitening offer", "offer": "Whitening from ₹8,000"})
    assert page.status_code == 201, page.text
    p = page.json()
    assert p["slug"] == "whitening-offer" and [s["id"] for s in p["sections"]][0] == "hero"
    assert (await ws.post("/marketing/landing-pages", {"name": "Whitening offer"})).status_code == 409
    slug = ws.org["slug"]
    assert (await client.get(f"/public/pages/{slug}/whitening-offer")).status_code == 404  # draft

    await ws.patch(f"/marketing/landing-pages/{p['id']}", {"status": "live"})
    pending = (await ws.get("/marketing/approvals")).json()
    assert pending[0]["kind"] == "landing"
    rej = await ws.post(f"/marketing/approvals/{pending[0]['id']}/decide", {"decision": "rejected"})
    assert rej.status_code == 400  # a comment is required
    await ws.post(f"/marketing/approvals/{pending[0]['id']}/decide", {"decision": "approved"})

    view = await client.get(f"/public/pages/{slug}/whitening-offer", params={"visitor": "v1"})
    assert view.status_code == 200 and view.json()["variant"] == "A" and view.json()["sections"]
    sub = await client.post(
        f"/public/pages/{slug}/whitening-offer/submit",
        json={"name": "Neha", "phone": "+919822200050", "marketingConsent": True, "utm": {"source": "meta"}},
    )
    assert sub.status_code == 201
    await drain()
    c = (await ws.get("/contacts", params={"q": "Neha"})).json()["items"][0]
    assert c["source"] == "landing:whitening-offer" and c["stage"] == "qualified"
    stats = (await ws.get(f"/marketing/landing-pages/{p['id']}")).json()
    assert stats["views"] == {"A": 1} and stats["conversions"] == {"A": 1} and stats["conversion"] == 100.0

    b = (await ws.post(f"/marketing/landing-pages/{p['id']}/rewrite", {"sectionId": "hero"})).json()
    assert "B" in b["sections"][0]["variants"]


async def test_aeo_and_demand_gen_with_approvals(ws):
    p = (
        await ws.post("/marketing/aeo/prompts", {"prompt": "best dentist in Pune", "competitors": ["Clove Dental"]})
    ).json()
    assert set(p["results"]) == {"chatgpt", "perplexity", "gemini", "aio"} and 0 <= p["score"] <= 100
    assert len(p["actions"]) >= 1 and len(p["history"]) == 1
    p2 = (await ws.post(f"/marketing/aeo/prompts/{p['id']}/check")).json()
    assert len(p2["history"]) == 2
    summary = (await ws.get("/marketing/aeo/summary")).json()
    assert summary["prompts"] == 1 and summary["engines"]["chatgpt"]["prompts"] == 1
    page = (await ws.post(f"/marketing/aeo/prompts/{p['id']}/draft-page")).json()
    assert page["type"] == "FAQ" and page["stage"] == "Draft"

    camp = await ws.post(
        "/marketing/campaigns",
        {"name": "Wedding whitening", "goal": "bookings", "budgetMinor": 5000000, "target": 40, "offer": "Whitening"},
    )
    assert camp.status_code == 201, camp.text
    c = camp.json()
    assert c["status"] == "planned" and c["channels"] and all(ch["status"] == "in_review" for ch in c["channels"])
    assert sum(ch["budgetMinor"] for ch in c["channels"]) == 5000000
    approvals = (await ws.get("/marketing/approvals")).json()
    budget = next(a for a in approvals if a["kind"] == "budget")
    await ws.post(f"/marketing/approvals/{budget['id']}/decide", {"decision": "approved"})
    after = (await ws.get(f"/marketing/campaigns/{c['id']}")).json()
    assert after["status"] == "active" and all(ch["status"] == "live" for ch in after["channels"])

    asset = next(a for a in (await ws.get("/marketing/approvals")).json() if a["kind"] == "ad")
    await ws.post(f"/marketing/approvals/{asset['id']}/decide", {"decision": "rejected", "comment": "No discounts"})
    mem = (await ws.get("/marketing/memory")).json()
    assert any("No discounts" in x for x in mem["clinic"]["losers"])
    done = (await ws.get("/marketing/approvals", params={"status": "decided"})).json()
    assert {a["status"] for a in done} == {"approved", "rejected"}


async def test_analytics_endpoints(ws, clinic):
    cid = await _contact(ws, source="google")
    start = (datetime.now(UTC) + timedelta(days=2)).replace(hour=5, minute=0, second=0, microsecond=0)
    await ws.post(
        "/appointments",
        {
            "contactId": cid,
            "serviceId": clinic["service"]["id"],
            "locationId": clinic["location"]["id"],
            "startsAt": start.isoformat(),
        },
    )
    pay = (
        await ws.post(
            "/payments", {"contactId": cid, "amountMinor": 250000, "purpose": "invoice", "description": "Visit"}
        )
    ).json()
    async with tenant_session(ws.org_uuid) as db:
        from app.models import Payment
        from app.services.payments import mark_paid

        await mark_paid(db, await db.get(Payment, uuid.UUID(pay["id"])), method="upi", provider_payment_id="t1")
        await db.commit()

    ov = (await ws.get("/analytics/overview", params={"range": "7d"})).json()
    assert ov["leads"] == 1 and ov["bookings"] == 1 and ov["revenueMinor"] == 250000 and ov["adSpendMinor"] > 0
    src = (await ws.get("/analytics/revenue-by-source")).json()
    assert src[0] == {"source": "google", "leads": 1, "bookings": 1, "customers": 1, "revenueMinor": 250000}
    for model in ("first", "last", "linear"):
        rows = (await ws.get("/analytics/attribution", params={"model": model})).json()
        assert sum(r["revenueMinor"] for r in rows) == 250000
    assert [s["count"] for s in (await ws.get("/analytics/funnel")).json()] == [1, 0, 1, 1]
    weekly = (await ws.get("/analytics/revenue-weekly", params={"weeks": 4})).json()
    assert len(weekly) == 4 and weekly[-1]["revenueMinor"] == 250000
    assert (await ws.get("/analytics/ai")).status_code == 200
    team = (await ws.get("/analytics/team")).json()
    assert team[0]["name"] == "Priya Sharma"
    assert (await ws.get("/analytics/outbound")).json()["funnel"][0]["stage"] == "Prospects found"


async def test_agent_studio_extras(ws):
    sc = await ws.post(
        "/agent/scenarios",
        {
            "name": "Price question",
            "messages": [{"role": "contact", "body": "How much is whitening?"}],
            "expect": {"mustNotInclude": ["guarantee"]},
        },
    )
    assert sc.status_code == 201, sc.text
    await ws.post("/agent/scenarios", {"name": "Hours", "messages": [{"role": "contact", "body": "Open Sunday?"}]})
    ran = (await ws.post("/agent/scenarios/run")).json()
    assert len(ran) == 2
    assert ran[0]["lastStatus"] in ("pass", "fail") and ran[0]["lastRunAt"]
    assert (await ws.get("/agent/scenarios")).json()[0]["lastDetail"]

    modes = (await ws.put("/agent/channel-modes", {"modes": {"webchat": "copilot"}})).json()
    assert modes["webchat"] == "copilot"
    assert (await ws.get("/agent/channel-modes")).json()["webchat"] == "copilot"
    assert (await ws.get("/agent/qa")).json() == []
    assert (await ws.get("/agent/knowledge-gaps")).json() == []
    faq = await ws.post("/agent/knowledge-gaps/answer", {"question": "Do you take insurance?", "answer": "Yes, most."})
    assert faq.status_code == 201


async def test_booking_settings_reminders_and_onboarding(ws, clinic, monkeypatch):
    s = await ws.put("/booking-settings", {"reminderHours": [24, 2], "minNoticeHours": 4, "maxDaysAhead": 30})
    assert s.json()["minNoticeHours"] == 4
    cid = await _contact(ws)
    start = (datetime.now(UTC) + timedelta(days=3)).replace(hour=6, minute=0, second=0, microsecond=0)
    r = await ws.post(
        "/appointments",
        {
            "contactId": cid,
            "serviceId": clinic["service"]["id"],
            "locationId": clinic["location"]["id"],
            "startsAt": start.isoformat(),
        },
    )
    assert r.status_code == 201
    async with tenant_session(ws.org_uuid) as db:
        reminders = (await db.scalars(select(Event).where(Event.type == "appointment.reminder"))).all()
        assert sorted(e.payload["hours"] for e in reminders) == [2, 24]
        for e in reminders:
            e.available_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    await drain()
    async with tenant_session(ws.org_uuid) as db:
        sent = (await db.scalars(select(Message.body).where(Message.author_name == "Reminder"))).all()
    assert len(sent) == 2 and "reminder of your" in sent[0]

    slots = (
        await ws.get(
            "/availability",
            params={"serviceId": clinic["service"]["id"], "start": datetime.now(UTC).date().isoformat(), "days": 31},
        )
    ).json()
    assert all(
        datetime.fromisoformat(x["startsAt"]) <= datetime.now(UTC) + timedelta(days=30, minutes=1) for x in slots
    )

    async def fake_assert(url: str) -> None:
        return None

    async def fake_crawl(url: str, max_pages: int = 6, client=None):  # noqa: ANN001, ANN202
        return [Page(url=url, title="Smile Dental | Pune", text="Teeth whitening, implants. Call us.")]

    monkeypatch.setattr(knowledge, "assert_public_url", fake_assert)
    monkeypatch.setattr(knowledge, "crawl", fake_crawl)
    imp = await ws.post("/onboarding/import-website", {"url": "smiledental.in"})
    assert imp.status_code == 200 and imp.json()["businessName"] == "Smile Dental"
    assert imp.json()["pages"][0]["url"] == "https://smiledental.in"
    assert ProspectRow  # model import keeps the table in metadata for TRUNCATE


async def test_sandbox_applies_escalation_rules_and_config_keeps_channel_modes(ws, make_workspace):
    await ws.post(
        "/agent/escalation-rules",
        {"name": "Legal threat", "kind": "keyword", "condition": {"keywords": ["consumer court"]}, "action": "assign"},
    )
    r = (
        await ws.post(
            "/agent/sandbox", {"messages": [{"role": "contact", "body": "Refund me or I go to consumer court"}]}
        )
    ).json()
    assert r["escalated"] is True and r["escalationReason"] == "Legal threat"

    await ws.put("/agent/channel-modes", {"modes": {"sms": "off"}})
    await ws.patch("/agent/config", {"guardrails": {"maxDiscountPct": 5}})
    assert (await ws.get("/agent/channel-modes")).json()["sms"] == "off"

    # Records of another workspace are invisible (row-level security), so writes by id fail with 404.
    pb = await ws.post("/agent/playbooks", {"name": "Mine", "instructions": "x"})
    other = await make_workspace("eve@other.in", "Other Clinic")
    hijack = await other.put(f"/agent/playbooks/{pb.json()['id']}", {"name": "Hijacked", "instructions": "y"})
    assert hijack.status_code == 404
    assert (await other.delete(f"/agent/playbooks/{pb.json()['id']}")).status_code == 404
    assert any(p["name"] == "Mine" for p in (await ws.get("/agent/playbooks")).json())

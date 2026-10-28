from datetime import UTC, datetime, timedelta


async def test_contact_crud_filters_and_timeline(ws):
    r = await ws.post(
        "/contacts",
        {
            "name": "Ananya Rao",
            "phone": "098220 11223",
            "email": "ananya@gmail.com",
            "tags": ["Whitening", "new patient"],
            "source": "Google Ads",
            "valueMinor": 1200000,
        },
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["phone"] == "+919822011223"
    assert c["tags"] == ["whitening", "new patient"]
    assert c["ownerId"] == "ai"

    dup = await ws.post("/contacts", {"name": "Dup", "phone": "+91 98220 11223"})
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "duplicate_phone"
    dup_email = await ws.post("/contacts", {"name": "Dup", "email": "ANANYA@gmail.com"})
    assert dup_email.status_code == 409

    await ws.post(
        "/contacts", {"name": "Rahul Verma", "company": "AgriGrow Inputs", "stage": "prospect", "tags": ["b2b"]}
    )
    assert (await ws.get("/contacts", params={"q": "agrigrow"})).json()["total"] == 1
    assert (await ws.get("/contacts", params={"q": "22011"})).json()["total"] == 1
    assert (await ws.get("/contacts", params={"stage": "prospect"})).json()["total"] == 1
    assert (await ws.get("/contacts", params={"tag": "whitening"})).json()["total"] == 1
    assert (await ws.get("/contacts", params={"owner": "ai", "sort": "value"})).json()["items"][0][
        "name"
    ] == "Ananya Rao"

    upd = await ws.patch(f"/contacts/{c['id']}", {"stage": "qualified"})
    assert upd.json()["stage"] == "qualified"

    note = await ws.post(f"/contacts/{c['id']}/notes", {"body": "Prefers Saturday mornings"})
    assert note.status_code == 201
    consent = await ws.put(
        f"/contacts/{c['id']}/consents", {"channel": "whatsapp", "status": "opted_in", "source": "Booking form"}
    )
    assert consent.json()["status"] == "opted_in"
    timeline = (await ws.get(f"/contacts/{c['id']}/timeline")).json()
    assert any(e["kind"] == "note" for e in timeline)

    audit = (await ws.get("/audit-log")).json()
    assert {"contact.created", "contact.stage_changed", "consent.updated"} <= {a["action"] for a in audit}


async def test_tasks_and_deals(ws):
    contact = (await ws.post("/contacts", {"name": "Kunal Shah"})).json()
    now = datetime.now(UTC)
    overdue = await ws.post(
        "/tasks",
        {"title": "Call about EMI", "contactId": contact["id"], "dueAt": (now - timedelta(hours=2)).isoformat()},
    )
    assert overdue.status_code == 201
    await ws.post(
        "/tasks", {"title": "Send brochure", "type": "whatsapp", "dueAt": (now + timedelta(days=3)).isoformat()}
    )
    assert (await ws.get("/tasks", params={"group": "overdue"})).json()["total"] == 1
    assert (await ws.get("/tasks", params={"group": "upcoming"})).json()["total"] == 1
    done = await ws.patch(f"/tasks/{overdue.json()['id']}", {"done": True})
    assert done.json()["done"] is True
    assert (await ws.get("/tasks", params={"group": "done"})).json()["total"] == 1

    deal = await ws.post(
        "/deals",
        {
            "contactId": contact["id"],
            "title": "Implants",
            "valueMinor": 12000000,
            "pipeline": "patients",
            "stage": "qualified",
        },
    )
    assert deal.json()["probability"] == 30
    won = await ws.patch(f"/deals/{deal.json()['id']}", {"stage": "won"})
    assert won.json()["probability"] == 100 and won.json()["closedAt"]
    assert (await ws.get(f"/contacts/{contact['id']}")).json()["stage"] == "customer"
    summary = {s["stage"]: s for s in (await ws.get("/deals/summary")).json()}
    assert summary["won"]["valueMinor"] == 12000000


async def test_large_b2b_values_do_not_overflow(ws):
    contact = (await ws.post("/contacts", {"name": "Big Dealer", "valueMinor": 50_000_000_000})).json()
    assert contact["valueMinor"] == 50_000_000_000  # ₹50 crore in paise
    await ws.post(
        "/deals",
        {"contactId": contact["id"], "title": "Statewide rollout", "valueMinor": 50_000_000_000, "stage": "proposal"},
    )
    summary = {s["stage"]: s for s in (await ws.get("/deals/summary")).json()}
    assert summary["proposal"]["weightedMinor"] == 35_000_000_000

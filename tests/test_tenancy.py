import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db.session import tenant_session
from app.models import Contact


async def test_api_isolates_tenants(make_workspace):
    a = await make_workspace("a@clinic-a.in", "Clinic A")
    b = await make_workspace("b@clinic-b.in", "Clinic B")
    created = await a.post("/contacts", {"name": "Ananya Rao", "phone": "+91 98220 11223"})
    assert created.status_code == 201
    cid = created.json()["id"]

    assert (await b.get(f"/contacts/{cid}")).status_code == 404
    assert (await b.get("/contacts")).json()["total"] == 0
    assert (await b.patch(f"/contacts/{cid}", {"name": "x"})).status_code == 404
    # Same phone is fine in another tenant: uniqueness is per org.
    assert (await b.post("/contacts", {"name": "Other", "phone": "+919822011223"})).status_code == 201


async def test_rls_blocks_cross_tenant_reads_and_writes(make_workspace):
    """Defence in depth: even a query with no org filter only sees the session's tenant."""
    a = await make_workspace("a@clinic-a.in", "Clinic A")
    b = await make_workspace("b@clinic-b.in", "Clinic B")
    await a.post("/contacts", {"name": "Only in A"})
    org_a, org_b = uuid.UUID(a.org["id"]), uuid.UUID(b.org["id"])

    async with tenant_session(org_b) as db:
        assert (await db.scalars(select(Contact))).all() == []
    async with tenant_session(org_a) as db:
        assert [c.name for c in (await db.scalars(select(Contact))).all()] == ["Only in A"]
    async with tenant_session(None) as db:  # no tenant pinned → nothing visible
        assert (await db.scalar(text("select count(*) from contacts"))) == 0

    async with tenant_session(org_b) as db:
        db.add(Contact(org_id=org_a, name="Smuggled", tags=[], utm={}, attributes={}))
        with pytest.raises(DBAPIError, match="row-level security"):
            await db.flush()

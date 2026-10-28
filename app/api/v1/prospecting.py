"""AI prospecting: plan a search from a prompt, run it against the configured data sources, enrich,
add AI research columns, then push rows to the CRM (optionally straight into a sequence)."""

import csv
import io
import uuid

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import DB, CurrentPrincipal
from app.core.errors import NotFound
from app.models import ProspectRow, ProspectSearch, Sequence
from app.schemas.common import Ok
from app.schemas.marketing import (
    AddToCrmIn,
    AiColumnIn,
    PlanIn,
    RowOut,
    RowsIn,
    SearchIn,
    SearchOut,
    SearchPatch,
    SearchPlan,
)
from app.services import prospecting, sequences
from app.services.events import emit

router = APIRouter(prefix="/prospecting", tags=["prospecting"])


async def _search(db: DB, search_id: uuid.UUID) -> ProspectSearch:
    s = await db.get(ProspectSearch, search_id)
    if s is None:
        raise NotFound("Search not found")
    return s


async def _rows(db: DB, search_id: uuid.UUID, ids: list[uuid.UUID]) -> list[ProspectRow]:
    return list(
        (await db.scalars(select(ProspectRow).where(ProspectRow.search_id == search_id, ProspectRow.id.in_(ids)))).all()
    )


@router.get("/sources", response_model=list[dict])
async def sources(principal: CurrentPrincipal, db: DB) -> list[dict]:
    from app.services.integrations import try_resolve_for_org
    from app.services.providers import is_sample

    out = []
    for sid, (label, kind, cost) in prospecting.SOURCES.items():
        p = await try_resolve_for_org(db, principal.org_id, kind)
        out.append(
            {
                "id": sid,
                "label": label,
                "kind": kind,
                "costPerRow": cost,
                "available": p is not None,
                "sample": p is not None and is_sample(p),
            }
        )
    return out


@router.get("/credits", response_model=dict)
async def credits(principal: CurrentPrincipal, db: DB) -> dict:
    from app.services import billing

    m = next(x for x in await billing.meters(db, principal.org_id, principal.org.plan) if x["id"] == "prospect")
    return {"limit": m["limit"], "used": m["used"], "remaining": m["limit"] - m["used"]}


@router.post("/plan", response_model=SearchPlan)
async def plan(body: PlanIn, principal: CurrentPrincipal) -> SearchPlan:
    """Turn a natural-language request into an editable search plan (no credits used)."""
    return SearchPlan.model_validate(await prospecting.plan(body.prompt))


@router.get("/searches", response_model=list[SearchOut])
async def list_searches(principal: CurrentPrincipal, db: DB) -> list[SearchOut]:
    rows = await db.scalars(
        select(ProspectSearch)
        .where(ProspectSearch.org_id == principal.org_id)
        .order_by(ProspectSearch.updated_at.desc())
    )
    return [SearchOut.model_validate(r) for r in rows]


@router.post("/searches", response_model=SearchOut, status_code=202)
async def create_search(body: SearchIn, principal: CurrentPrincipal, db: DB) -> SearchOut:
    s = ProspectSearch(
        org_id=principal.org_id,
        name=body.name,
        prompt=body.prompt,
        plan=body.plan.model_dump(by_alias=True),
        monitor=body.monitor,
        status="running",
        ai_columns=[],
        created_by_id=principal.user_id,
    )
    db.add(s)
    await db.flush()
    emit(db, principal.org_id, "prospect_search.run", {"search_id": s.id})
    await db.commit()
    return SearchOut.model_validate(s)


@router.get("/searches/{search_id}", response_model=SearchOut)
async def get_search(search_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SearchOut:
    return SearchOut.model_validate(await _search(db, search_id))


@router.patch("/searches/{search_id}", response_model=SearchOut)
async def patch_search(search_id: uuid.UUID, body: SearchPatch, principal: CurrentPrincipal, db: DB) -> SearchOut:
    s = await _search(db, search_id)
    if body.name is not None:
        s.name = body.name
    if body.monitor is not None:
        s.monitor = body.monitor
    await db.commit()
    return SearchOut.model_validate(s)


@router.post("/searches/{search_id}/run", response_model=SearchOut, status_code=202)
async def rerun(search_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> SearchOut:
    s = await _search(db, search_id)
    s.status = "running"
    emit(db, principal.org_id, "prospect_search.run", {"search_id": s.id})
    await db.commit()
    return SearchOut.model_validate(s)


@router.delete("/searches/{search_id}", response_model=Ok)
async def delete_search(search_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    await db.delete(await _search(db, search_id))
    await db.commit()
    return Ok()


@router.get("/searches/{search_id}/rows", response_model=list[RowOut])
async def rows(
    search_id: uuid.UUID,
    principal: CurrentPrincipal,
    db: DB,
    min_fit: int = Query(0, ge=0, le=100),
    verified_only: bool = False,
    q: str | None = None,
    limit: int = Query(500, le=2000),
) -> list[RowOut]:
    cond = [ProspectRow.search_id == search_id, ProspectRow.fit >= min_fit]
    if verified_only:
        cond.append((ProspectRow.email_status == "verified") | (ProspectRow.phone_status == "verified"))
    if q:
        cond.append(ProspectRow.company.ilike(f"%{q}%") | ProspectRow.contact_name.ilike(f"%{q}%"))
    res = await db.scalars(select(ProspectRow).where(*cond).order_by(ProspectRow.fit.desc()).limit(limit))
    return [RowOut.model_validate(r) for r in res]


@router.get("/searches/{search_id}/export.csv")
async def export(search_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> StreamingResponse:
    s = await _search(db, search_id)
    res = (
        await db.scalars(select(ProspectRow).where(ProspectRow.search_id == s.id).order_by(ProspectRow.fit.desc()))
    ).all()
    buf = io.StringIO()
    cols = [
        "company",
        "city",
        "website",
        "rating",
        "contact_name",
        "role",
        "email",
        "email_status",
        "phone",
        "phone_status",
        "fit",
    ]
    w = csv.writer(buf)
    w.writerow([*cols, *[c["question"] for c in s.ai_columns]])
    for r in res:
        w.writerow(
            [getattr(r, c) or "" for c in cols] + [(r.ai or {}).get(c["id"], {}).get("value", "") for c in s.ai_columns]
        )
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{prospecting_filename(s.name)}.csv"'},
    )


def prospecting_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:60] or "prospects"


@router.post("/searches/{search_id}/enrich", response_model=dict)
async def enrich(search_id: uuid.UUID, body: RowsIn, principal: CurrentPrincipal, db: DB) -> dict:
    """Enrichment waterfall over the configured providers (Apollo, Hunter, …). Charges 1 credit per value found."""
    await _search(db, search_id)
    stats = await prospecting.enrich(db, principal.org, await _rows(db, search_id, body.row_ids))
    await db.commit()
    return stats


@router.post("/searches/{search_id}/columns", response_model=SearchOut, status_code=202)
async def add_column(search_id: uuid.UUID, body: AiColumnIn, principal: CurrentPrincipal, db: DB) -> SearchOut:
    s = await _search(db, search_id)
    col = {"id": f"c{len(s.ai_columns) + 1}", "question": body.question}
    s.ai_columns = [*s.ai_columns, col]
    emit(db, principal.org_id, "prospect_search.ai_column", {"search_id": s.id, "column_id": col["id"]})
    await db.commit()
    return SearchOut.model_validate(s)


@router.delete("/searches/{search_id}/columns/{column_id}", response_model=SearchOut)
async def remove_column(search_id: uuid.UUID, column_id: str, principal: CurrentPrincipal, db: DB) -> SearchOut:
    s = await _search(db, search_id)
    s.ai_columns = [c for c in s.ai_columns if c["id"] != column_id]
    await db.commit()
    return SearchOut.model_validate(s)


@router.post("/searches/{search_id}/add-to-crm", response_model=dict)
async def add_to_crm(search_id: uuid.UUID, body: AddToCrmIn, principal: CurrentPrincipal, db: DB) -> dict:
    await _search(db, search_id)
    rows_ = await _rows(db, search_id, body.row_ids)
    ids = await prospecting.add_to_crm(
        db, principal.org, rows_, owner_id=body.owner_id or principal.user_id, tag=body.tag
    )
    await db.flush()
    enrolled = 0
    if body.sequence_id and ids:
        seq = await db.get(Sequence, body.sequence_id)
        if seq is None:
            raise NotFound("Sequence not found")
        enrolled = (await sequences.enroll(db, principal.org, seq, ids, principal.user_id))["enrolled"]
    await db.commit()
    return {
        "added": len(ids),
        "skipped": len(rows_) - len(ids),
        "enrolled": enrolled,
        "contactIds": [str(i) for i in ids],
    }

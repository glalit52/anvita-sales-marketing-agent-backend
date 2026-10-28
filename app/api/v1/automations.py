"""Workflows: CRUD, templates, run history, manual test runs and an API trigger for external events."""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, CurrentPrincipal, ManagerPrincipal
from app.core.errors import AppError, NotFound
from app.models import Contact, Workflow, WorkflowRun
from app.schemas.common import Ok
from app.schemas.marketing import CustomEventIn, RunOut, WorkflowIn, WorkflowOut
from app.services import automations as auto
from app.services.audit import audit
from app.services.contacts import get_or_create_contact
from app.services.events import emit

router = APIRouter(prefix="/automations", tags=["automations"])


async def _wf(db: DB, workflow_id: uuid.UUID) -> Workflow:
    wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise NotFound("Workflow not found")
    return wf


async def _out(db: DB, wf: Workflow) -> WorkflowOut:
    out = WorkflowOut.model_validate(wf)
    row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(WorkflowRun.status.in_(["success", "branched"])),
                func.count().filter(WorkflowRun.status == "failed"),
                func.max(WorkflowRun.created_at),
            ).where(WorkflowRun.workflow_id == wf.id)
        )
    ).one()
    out.runs, out.last_run_at = row[0], row[3]
    finished = row[1] + row[2]
    out.success_rate = round(100 * row[1] / finished) if finished else 100
    return out


def _check(body: WorkflowIn) -> None:
    if body.trigger.get("type") not in {t["id"] for t in auto.TRIGGERS}:
        raise AppError("Unknown trigger", code="invalid_workflow")
    auto.validate(body.nodes)


@router.get("/meta", response_model=dict)
async def meta(principal: CurrentPrincipal) -> dict:
    return {
        "triggers": auto.TRIGGERS,
        "actions": auto.ACTIONS,
        "templates": [{k: t[k] for k in ("id", "name", "category", "description")} for t in auto.TEMPLATES],
    }


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(principal: CurrentPrincipal, db: DB) -> list[WorkflowOut]:
    rows = await db.scalars(select(Workflow).where(Workflow.org_id == principal.org_id).order_by(Workflow.created_at))
    return [await _out(db, w) for w in rows]


@router.post("", response_model=WorkflowOut, status_code=201)
async def create(body: WorkflowIn, principal: ManagerPrincipal, db: DB) -> WorkflowOut:
    _check(body)
    wf = Workflow(org_id=principal.org_id, **body.model_dump(by_alias=False))
    db.add(wf)
    await db.commit()
    return await _out(db, wf)


@router.post("/from-template/{template_id}", response_model=WorkflowOut, status_code=201)
async def from_template(template_id: str, principal: ManagerPrincipal, db: DB) -> WorkflowOut:
    t = next((t for t in auto.TEMPLATES if t["id"] == template_id), None)
    if t is None:
        raise NotFound("Template not found")
    wf = Workflow(
        org_id=principal.org_id,
        name=t["name"],
        description=t["description"],
        category=t["category"],
        trigger=t["trigger"],
        nodes=t["nodes"],
        enabled=False,
    )
    db.add(wf)
    await db.commit()
    return await _out(db, wf)


@router.get("/runs", response_model=list[RunOut])
async def runs(
    principal: CurrentPrincipal,
    db: DB,
    workflow_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
) -> list[RunOut]:
    q = (
        select(WorkflowRun, Workflow.name, Workflow.nodes, Contact.name)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .outerjoin(Contact, Contact.id == WorkflowRun.contact_id)
        .where(WorkflowRun.org_id == principal.org_id)
    )
    if workflow_id:
        q = q.where(WorkflowRun.workflow_id == workflow_id)
    if status:
        q = q.where(WorkflowRun.status == status)
    out = []
    for run, wname, nodes, cname in await db.execute(q.order_by(WorkflowRun.created_at.desc()).limit(limit)):
        item = RunOut.model_validate(run)
        item.workflow_name, item.contact_name = wname, cname
        item.total_steps = len([n for n in nodes or [] if n.get("kind") != "trigger"])
        out.append(item)
    return out


@router.post("/events", response_model=dict, status_code=202)
async def custom_event(body: CustomEventIn, principal: CurrentPrincipal, db: DB) -> dict:
    """Start workflows with an 'API event' trigger (use an API key with automations:write). Pass a contactId,
    or a phone/email to match or create the contact."""
    contact_id = body.contact_id
    if contact_id is None and (body.phone or body.email):
        c, _ = await get_or_create_contact(
            db, principal.org_id, name=body.name, phone=body.phone, email=body.email, source="api"
        )
        contact_id = c.id
    emit(
        db,
        principal.org_id,
        "automation.custom",
        {"event": body.event, "contact_id": contact_id, **{f"data_{k}": v for k, v in body.data.items()}},
    )
    await db.commit()
    return {"queued": True, "contactId": str(contact_id) if contact_id else None}


@router.get("/{workflow_id}", response_model=WorkflowOut)
async def get(workflow_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> WorkflowOut:
    return await _out(db, await _wf(db, workflow_id))


@router.put("/{workflow_id}", response_model=WorkflowOut)
async def update(workflow_id: uuid.UUID, body: WorkflowIn, principal: ManagerPrincipal, db: DB) -> WorkflowOut:
    _check(body)
    wf = await _wf(db, workflow_id)
    for k, v in body.model_dump(by_alias=False).items():
        setattr(wf, k, v)
    audit(db, principal.org_id, "workflow.updated", actor_id=principal.user_id, entity_type="workflow", entity_id=wf.id)
    await db.commit()
    return await _out(db, wf)


@router.post("/{workflow_id}/toggle", response_model=WorkflowOut)
async def toggle(
    workflow_id: uuid.UUID, principal: ManagerPrincipal, db: DB, enabled: bool = Query(...)
) -> WorkflowOut:
    wf = await _wf(db, workflow_id)
    if enabled and not [n for n in wf.nodes if n.get("kind") != "trigger"]:
        raise AppError("Add at least one step first", code="no_steps")
    wf.enabled = enabled
    audit(
        db,
        principal.org_id,
        "workflow.enabled" if enabled else "workflow.disabled",
        actor_id=principal.user_id,
        entity_type="workflow",
        entity_id=wf.id,
    )
    await db.commit()
    return await _out(db, wf)


@router.post("/{workflow_id}/test", response_model=RunOut, status_code=202)
async def test_run(
    workflow_id: uuid.UUID, principal: ManagerPrincipal, db: DB, contact_id: uuid.UUID | None = None
) -> RunOut:
    """Run the workflow now for one contact (real actions, like a live run), regardless of its trigger."""
    wf = await _wf(db, workflow_id)
    run = auto.start_run(db, wf, contact_id, {"manual": True, "contact_id": contact_id}, None)
    await db.flush()
    emit(db, principal.org_id, "workflow.step", {"run_id": run.id})
    await db.commit()
    return RunOut.model_validate(run)


@router.delete("/{workflow_id}", response_model=Ok)
async def delete(workflow_id: uuid.UUID, principal: ManagerPrincipal, db: DB) -> Ok:
    await db.delete(await _wf(db, workflow_id))
    await db.commit()
    return Ok()

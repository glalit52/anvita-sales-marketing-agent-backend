"""Tasks and the deal pipeline."""

import uuid
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, CurrentPrincipal
from app.core.errors import AppError, NotFound
from app.db.base import utcnow
from app.models import Contact, Deal, Task
from app.models.crm import DEAL_PROBABILITY, DEAL_STAGES
from app.schemas.common import Ok, Page
from app.schemas.crm import DealIn, DealOut, DealUpdate, PipelineStageSummary, TaskIn, TaskOut, TaskUpdate
from app.services.audit import audit

router = APIRouter(tags=["sell"])


def _task_out(t: Task) -> TaskOut:
    return TaskOut.model_validate(
        {**{c.key: getattr(t, c.key) for c in Task.__table__.columns}, "done": t.done_at is not None}
    )


@router.get("/tasks", response_model=Page[TaskOut])
async def list_tasks(
    principal: CurrentPrincipal,
    db: DB,
    group: Literal["all", "overdue", "today", "upcoming", "done"] = "all",
    assignee: str | None = Query(None, description="User id or 'me'"),
    contact_id: uuid.UUID | None = None,
    created_by: Literal["ai", "user"] | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[TaskOut]:
    cond = [Task.org_id == principal.org_id]
    tz = ZoneInfo(principal.org.timezone)
    now = utcnow()
    start_today = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz)
    end_today = start_today + timedelta(days=1)
    if group == "done":
        cond.append(Task.done_at.is_not(None))
    elif group != "all":
        cond.append(Task.done_at.is_(None))
        if group == "overdue":
            cond.append(Task.due_at < now)
        elif group == "today":
            cond += [Task.due_at >= now, Task.due_at < end_today]
        elif group == "upcoming":
            cond.append(Task.due_at >= end_today)
    if assignee == "me":
        cond.append(Task.assignee_id == principal.user_id)
    elif assignee:
        try:
            cond.append(Task.assignee_id == uuid.UUID(assignee))
        except ValueError as e:
            raise AppError("assignee must be a user id or 'me'", code="invalid_assignee") from e
    if contact_id:
        cond.append(Task.contact_id == contact_id)
    if created_by:
        cond.append(Task.created_by_type == created_by)
    total = await db.scalar(select(func.count()).select_from(Task).where(*cond)) or 0
    order = Task.done_at.desc() if group == "done" else Task.due_at.asc()
    rows = await db.scalars(select(Task).where(*cond).order_by(order, Task.id).limit(limit).offset(offset))
    return Page(items=[_task_out(t) for t in rows], total=total, limit=limit, offset=offset)


@router.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task(body: TaskIn, principal: CurrentPrincipal, db: DB) -> TaskOut:
    task = Task(
        org_id=principal.org_id,
        created_by_type="user",
        created_by_id=principal.user_id,
        **body.model_dump(by_alias=False),
    )
    if task.assignee_id is None:
        task.assignee_id = principal.user_id
    db.add(task)
    await db.commit()
    return _task_out(task)


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, body: TaskUpdate, principal: CurrentPrincipal, db: DB) -> TaskOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise NotFound("Task not found")
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    if "done" in changes:
        task.done_at = utcnow() if changes.pop("done") else None
    for k, v in changes.items():
        setattr(task, k, v)
    await db.commit()
    return _task_out(task)


@router.delete("/tasks/{task_id}", response_model=Ok)
async def delete_task(task_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    task = await db.get(Task, task_id)
    if task is None:
        raise NotFound("Task not found")
    await db.delete(task)
    await db.commit()
    return Ok()


# ---- Deals -----------------------------------------------------------------------------------------


@router.get("/deals", response_model=Page[DealOut])
async def list_deals(
    principal: CurrentPrincipal,
    db: DB,
    pipeline: str | None = None,
    stage: list[str] | None = Query(None),
    contact_id: uuid.UUID | None = None,
    owner: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[DealOut]:
    cond = [Deal.org_id == principal.org_id]
    if pipeline:
        cond.append(Deal.pipeline == pipeline)
    if stage:
        cond.append(Deal.stage.in_(stage))
    if contact_id:
        cond.append(Deal.contact_id == contact_id)
    if owner == "me":
        cond.append(Deal.owner_id == principal.user_id)
    total = await db.scalar(select(func.count()).select_from(Deal).where(*cond)) or 0
    rows = await db.scalars(
        select(Deal).where(*cond).order_by(Deal.updated_at.desc(), Deal.id).limit(limit).offset(offset)
    )
    return Page(items=[DealOut.model_validate(d) for d in rows], total=total, limit=limit, offset=offset)


@router.get("/deals/summary", response_model=list[PipelineStageSummary])
async def deals_summary(principal: CurrentPrincipal, db: DB, pipeline: str | None = None) -> list[PipelineStageSummary]:
    cond = [Deal.org_id == principal.org_id]
    if pipeline:
        cond.append(Deal.pipeline == pipeline)
    rows = await db.execute(
        select(
            Deal.stage,
            func.count(),
            func.coalesce(func.sum(Deal.value_minor), 0),
            func.coalesce(func.sum(Deal.value_minor * Deal.probability / 100), 0),
        )
        .where(*cond)
        .group_by(Deal.stage)
    )
    by_stage = {r[0]: r for r in rows}
    return [
        PipelineStageSummary(
            stage=s,
            count=by_stage[s][1] if s in by_stage else 0,
            value_minor=int(by_stage[s][2]) if s in by_stage else 0,
            weighted_minor=int(by_stage[s][3]) if s in by_stage else 0,
        )
        for s in DEAL_STAGES
    ]


@router.post("/deals", response_model=DealOut, status_code=201)
async def create_deal(body: DealIn, principal: CurrentPrincipal, db: DB) -> DealOut:
    contact = await db.get(Contact, body.contact_id)
    if contact is None:
        raise NotFound("Contact not found")
    data = body.model_dump(by_alias=False)
    data["probability"] = data["probability"] if data["probability"] is not None else DEAL_PROBABILITY[body.stage]
    deal = Deal(org_id=principal.org_id, **data)
    if deal.stage in ("won", "lost"):
        deal.closed_at = utcnow()
    db.add(deal)
    await db.commit()
    return DealOut.model_validate(deal)


@router.get("/deals/{deal_id}", response_model=DealOut)
async def get_deal(deal_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> DealOut:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise NotFound("Deal not found")
    return DealOut.model_validate(deal)


@router.patch("/deals/{deal_id}", response_model=DealOut)
async def update_deal(deal_id: uuid.UUID, body: DealUpdate, principal: CurrentPrincipal, db: DB) -> DealOut:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise NotFound("Deal not found")
    changes = body.model_dump(exclude_unset=True, by_alias=False)
    old_stage = deal.stage
    for k, v in changes.items():
        setattr(deal, k, v)
    if "stage" in changes and deal.stage != old_stage:
        if "probability" not in changes:
            deal.probability = DEAL_PROBABILITY[deal.stage]
        deal.closed_at = utcnow() if deal.stage in ("won", "lost") else None
        if deal.stage == "won":
            contact = await db.get(Contact, deal.contact_id)
            if contact and contact.stage != "customer":
                contact.stage = "customer"
        audit(
            db,
            principal.org_id,
            "deal.stage_changed",
            actor_id=principal.user_id,
            entity_type="deal",
            entity_id=deal.id,
            data={"from": old_stage, "to": deal.stage},
        )
    await db.commit()
    return DealOut.model_validate(deal)


@router.delete("/deals/{deal_id}", response_model=Ok)
async def delete_deal(deal_id: uuid.UUID, principal: CurrentPrincipal, db: DB) -> Ok:
    deal = await db.get(Deal, deal_id)
    if deal is None:
        raise NotFound("Deal not found")
    await db.delete(deal)
    await db.commit()
    return Ok()

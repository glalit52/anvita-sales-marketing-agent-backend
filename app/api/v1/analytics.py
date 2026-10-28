"""Reports. Every endpoint reads live data; `range` is 7d, 30d or 90d."""

from typing import Literal

from fastapi import APIRouter

from app.api.deps import DB, CurrentPrincipal
from app.services import analytics

router = APIRouter(prefix="/analytics", tags=["analytics"])
Range = Literal["7d", "30d", "90d"]


@router.get("/overview", response_model=dict)
async def overview(principal: CurrentPrincipal, db: DB, range: Range = "30d") -> dict:  # noqa: A002
    return await analytics.overview(db, principal.org_id, range)


@router.get("/revenue-by-source", response_model=list[dict])
async def revenue_by_source(principal: CurrentPrincipal, db: DB, range: Range = "30d") -> list[dict]:  # noqa: A002
    return await analytics.revenue_by_source(db, principal.org_id, range)


@router.get("/revenue-weekly", response_model=list[dict])
async def revenue_weekly(principal: CurrentPrincipal, db: DB, weeks: int = 12) -> list[dict]:
    return await analytics.weekly_revenue(db, principal.org_id, min(max(weeks, 1), 52))


@router.get("/attribution", response_model=list[dict])
async def attribution(
    principal: CurrentPrincipal,
    db: DB,
    model: Literal["first", "last", "linear"] = "linear",
    range: Range = "30d",  # noqa: A002
) -> list[dict]:
    return await analytics.attribution(db, principal.org_id, model, range)


@router.get("/funnel", response_model=list[dict])
async def funnel(principal: CurrentPrincipal, db: DB, range: Range = "30d") -> list[dict]:  # noqa: A002
    return await analytics.funnel(db, principal.org_id, range)


@router.get("/ai", response_model=dict)
async def ai(principal: CurrentPrincipal, db: DB) -> dict:
    return await analytics.ai_performance(db, principal.org_id)


@router.get("/team", response_model=list[dict])
async def team(principal: CurrentPrincipal, db: DB, range: Range = "30d") -> list[dict]:  # noqa: A002
    return await analytics.team(db, principal.org_id, range)


@router.get("/outbound", response_model=dict)
async def outbound(principal: CurrentPrincipal, db: DB, range: Range = "30d") -> dict:  # noqa: A002
    return await analytics.outbound(db, principal.org_id, range)

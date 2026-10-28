from fastapi import APIRouter

from app.api.v1 import (
    agent,
    analytics,
    auth,
    automations,
    calendar,
    contacts,
    conversations,
    growth,
    marketing,
    onboarding,
    ops,
    org,
    payments,
    platform,
    prospecting,
    public,
    sell,
    webhooks,
)

api_router = APIRouter()
for module in (
    auth,
    org,
    contacts,
    conversations,
    sell,
    growth,
    prospecting,
    marketing,
    automations,
    analytics,
    onboarding,
    calendar,
    payments,
    agent,
    ops,
    platform,
    public,
    webhooks,
):
    api_router.include_router(module.router)

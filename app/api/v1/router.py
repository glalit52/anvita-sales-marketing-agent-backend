from fastapi import APIRouter

from app.api.v1 import (
    agent,
    auth,
    calendar,
    contacts,
    conversations,
    ops,
    org,
    payments,
    platform,
    public,
    sell,
    webhooks,
)

api_router = APIRouter()
for module in (auth, org, contacts, conversations, sell, calendar, payments, agent, ops, platform, public, webhooks):
    api_router.include_router(module.router)

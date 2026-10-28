"""Outbox worker: `python -m app.worker`.

Claims due events with SELECT … FOR UPDATE SKIP LOCKED (so any number of workers can run side by side),
leases them, runs the handler in its own tenant-scoped transaction, and retries with backoff.
"""

import asyncio
import contextlib
import logging
import signal
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import configure_logging, org_id_var
from app.db.base import utcnow
from app.db.session import tenant_session
from app.models import (
    Appointment,
    Campaign,
    Channel,
    Contact,
    Conversation,
    Event,
    KnowledgeSource,
    Message,
    Organization,
    ProspectSearch,
    ReviewRequest,
    Service,
    SocialPost,
    WebhookEndpoint,
)
from app.services import automations, campaigns, marketing, prospecting, reviews, sequences
from app.services.agent.runtime import run_reply
from app.services.channels import SendError, adapter_for
from app.services.events import emit
from app.services.inbox import create_outbound, open_conversation, window_open
from app.services.knowledge import ingest_source
from app.services.webhooks_out import PUBLIC_EVENTS, deliver, fanout

log = logging.getLogger("anvita.worker")
LEASE = timedelta(minutes=5)

Handler = Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handles(event_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[event_type] = fn
        return fn

    return deco


@handles("message.received")
async def on_message_received(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        msg = await db.get(Message, uuid.UUID(payload["message_id"]))
        conv = await db.get(Conversation, uuid.UUID(payload["conversation_id"]))
        if msg is not None and conv is not None and msg.author_type == "contact":
            await sequences.on_inbound(db, org_id, conv.contact_id, msg)
            await db.commit()
        await run_reply(db, org_id, uuid.UUID(payload["conversation_id"]), uuid.UUID(payload["message_id"]))
        await db.commit()


@handles("campaign.send")
async def on_campaign_send(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        campaign = await db.get(Campaign, uuid.UUID(payload["campaign_id"]), with_for_update=True)
        if campaign is None:
            return
        more = await campaigns.send_batch(db, campaign)
        if more:
            emit(db, org_id, "campaign.send", {"campaign_id": campaign.id})
        await db.commit()


@handles("sequence.step")
async def on_sequence_step(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        await sequences.run_step(db, uuid.UUID(payload["enrollment_id"]), int(payload["position"]))
        await db.commit()


@handles("sequence.classify_reply")
async def on_classify_reply(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        await sequences.classify_reply(db, uuid.UUID(payload["enrollment_id"]), uuid.UUID(payload["message_id"]))
        await db.commit()


@handles("appointment.completed")
async def on_appointment_completed(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Auto review request after a completed visit (Settings → Reviews → automatic requests)."""
    async with tenant_session(org_id) as db:
        org = await db.get(Organization, org_id)
        appt = await db.get(Appointment, uuid.UUID(payload["appointment_id"]))
        if org is None or appt is None or appt.status != "completed":
            return
        cfg = (org.settings or {}).get("reviews", {})
        if not cfg.get("auto_request", True):
            return
        if await db.scalar(select(ReviewRequest.id).where(ReviewRequest.appointment_id == appt.id)):
            return
        contact = await db.get(Contact, appt.contact_id)
        if contact is None:
            return
        channel = "whatsapp" if contact.phone else "email"
        await reviews.send_request(db, org, contact, channel=cfg.get("channel", channel), appointment_id=appt.id)
        await db.commit()


class PermanentFailure(Exception):
    pass


@handles("message.outbound")
async def on_message_outbound(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        msg = await db.get(Message, uuid.UUID(payload["message_id"]), with_for_update=True)
        if msg is None or msg.status != "queued":
            return  # already delivered (at-least-once delivery means duplicates are normal)
        conv = await db.get(Conversation, msg.conversation_id)
        assert conv is not None
        contact = await db.get(Contact, conv.contact_id)
        channel = await db.get(Channel, conv.channel_id) if conv.channel_id else None
        assert contact is not None
        try:
            msg.provider_msg_id = await adapter_for(conv.channel_type).send(channel, contact, msg)
            msg.status = "sent"
            msg.error = None
        except SendError as e:
            msg.error = str(e)[:500]
            if not e.retryable:
                msg.status = "failed"
                await db.commit()
                raise PermanentFailure(str(e)) from e
            await db.commit()
            raise
        await db.commit()


@handles("webhook.deliver")
async def on_webhook_deliver(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        endpoint = await db.get(WebhookEndpoint, uuid.UUID(payload["endpoint_id"]))
        if endpoint is None or not endpoint.active:
            return
        ok = await deliver(db, endpoint, payload["event"], payload["event_id"], payload.get("data", {}))
        await db.commit()
    if not ok:
        raise RuntimeError(f"Webhook delivery failed: {endpoint.last_error}")  # retried with backoff


@handles("knowledge_source.created")
async def on_kb_source(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        source = await db.get(KnowledgeSource, uuid.UUID(payload["source_id"]))
        if source is None:
            return
        await ingest_source(db, source)
        await db.commit()


@handles("prospect_search.run")
async def on_prospect_search(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        search = await db.get(ProspectSearch, uuid.UUID(payload["search_id"]), with_for_update=True)
        org = await db.get(Organization, org_id)
        if search is None or org is None:
            return
        try:
            await prospecting.run(db, org, search)
        except Exception as e:  # noqa: BLE001 - a failed search is shown on the search, not retried forever
            await db.rollback()
            search = await db.get(ProspectSearch, uuid.UUID(payload["search_id"]))
            if search is None:
                return
            search.status, search.error = "failed", (getattr(e, "message", None) or str(e))[:500]
        await db.commit()


@handles("prospect_search.ai_column")
async def on_ai_column(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        search = await db.get(ProspectSearch, uuid.UUID(payload["search_id"]))
        org = await db.get(Organization, org_id)
        if search and org:
            await prospecting.answer_column(db, org, search, payload["column_id"])
            await db.commit()


@handles("social.publish")
async def on_social_publish(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        post = await db.get(SocialPost, uuid.UUID(payload["post_id"]), with_for_update=True)
        # Skip if unscheduled, already published, or rescheduled since this event was queued.
        if post is None or post.status != "scheduled":
            return
        if post.scheduled_at and payload.get("at") and datetime.fromisoformat(str(payload["at"])) != post.scheduled_at:
            return
        await marketing.publish_post(db, post)
        await db.commit()


@handles("workflow.step")
async def on_workflow_step(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        await automations.step(db, uuid.UUID(payload["run_id"]))
        await db.commit()


@handles("appointment.reminder")
async def on_appointment_reminder(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        appt = await db.get(Appointment, uuid.UUID(payload["appointment_id"]))
        org = await db.get(Organization, org_id)
        if appt is None or org is None or appt.status in ("cancelled", "completed", "no-show"):
            return
        if payload.get("starts_at") and datetime.fromisoformat(str(payload["starts_at"])) != appt.starts_at:
            return  # rescheduled: the new booking queued its own reminders
        contact = await db.get(Contact, appt.contact_id)
        service = await db.get(Service, appt.service_id)
        if contact is None or not (contact.phone or contact.email):
            return
        cfg = (org.settings or {}).get("booking") or {}
        channel = cfg.get("reminder_channel") or contact.channel or ("whatsapp" if contact.phone else "email")
        if channel in ("whatsapp", "sms") and not contact.phone:
            channel = "email"
        if channel == "email" and not contact.email:
            channel = "sms"
        when = appt.starts_at.astimezone(ZoneInfo(org.timezone)).strftime("%a %d %b, %I:%M %p")
        first = (contact.name or "").split(" ")[0] or "there"
        body = (
            f"Hi {first}, a reminder of your {service.name if service else 'appointment'} at {org.name} on {when}. "
            "Reply C to confirm or R to reschedule."
        )
        conv = await open_conversation(db, org_id, contact, channel, None)
        await create_outbound(
            db,
            conv,
            body=body,
            author_type="ai",
            author_name="Reminder",
            pause_ai=False,
            idempotency_key=f"reminder:{appt.id}:{payload.get('hours')}",
            template=None
            if channel != "whatsapp" or window_open(conv)
            else {"name": "appointment_reminder", "language": "en", "params": [first, when]},
        )
        await db.commit()


async def start_automations(event: Event) -> None:
    """Start workflow runs for this event, once (first attempt), like webhook fan-out."""
    if event.org_id is None or event.attempts != 1 or event.type not in automations.EVENT_TRIGGERS:
        return
    async with tenant_session(event.org_id) as db:
        await automations.on_event(db, event.org_id, event.type, event.payload, event.id)
        await db.commit()


async def claim(batch: int) -> list[Event]:
    async with tenant_session(bypass_rls=True) as db:
        now = utcnow()
        rows = list(
            (
                await db.scalars(
                    select(Event)
                    .where(
                        Event.processed_at.is_(None),
                        Event.available_at <= now,
                        (Event.locked_until.is_(None)) | (Event.locked_until < now),
                    )
                    .order_by(Event.available_at)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for e in rows:
            e.locked_until = now + LEASE
            e.attempts += 1
        await db.commit()
        return rows


async def finish(
    event_id: uuid.UUID, *, error: str | None = None, retry_in: float | None = None, give_up: bool = False
) -> None:
    async with tenant_session(bypass_rls=True) as db:
        values: dict[str, Any] = {"locked_until": None, "last_error": error}
        if error is None or give_up:
            values["processed_at"] = utcnow()
        elif retry_in is not None:
            values["available_at"] = utcnow() + timedelta(seconds=retry_in)
        await db.execute(update(Event).where(Event.id == event_id).values(**values))
        await db.commit()


async def publish_to_webhooks(event: Event) -> None:
    """Queue one delivery per subscribed endpoint, on the first attempt only (retries must not re-fan)."""
    if event.org_id is None or event.attempts != 1 or event.type not in PUBLIC_EVENTS:
        return
    async with tenant_session(event.org_id) as db:
        await fanout(db, event.org_id, event.type, event.payload, event.id)
        await db.commit()


async def process(event: Event) -> None:
    org_id_var.set(str(event.org_id) if event.org_id else None)
    try:
        await publish_to_webhooks(event)
    except Exception:  # noqa: BLE001 - webhook fan-out must never block the domain handler
        log.exception("webhook fanout failed", extra={"data": {"event": event.type}})
    try:
        await start_automations(event)
    except Exception:  # noqa: BLE001 - a broken workflow must never block the domain handler
        log.exception("automation dispatch failed", extra={"data": {"event": event.type}})
    handler = HANDLERS.get(event.type)
    if handler is None:
        await finish(event.id)  # nothing else subscribes to this event type
        return
    max_attempts = get_settings().worker_max_attempts
    try:
        assert event.org_id is not None
        await handler(event.org_id, event.payload)
    except PermanentFailure as e:
        await finish(event.id, error=str(e), give_up=True)
    except Exception as e:  # noqa: BLE001 - any failure is recorded on the event and retried
        give_up = event.attempts >= max_attempts
        log.exception(
            "event handler failed",
            extra={"data": {"event": event.type, "id": str(event.id), "attempt": event.attempts, "give_up": give_up}},
        )
        await finish(
            event.id, error=f"{type(e).__name__}: {e}"[:2000], retry_in=min(2**event.attempts * 5, 600), give_up=give_up
        )
    else:
        await finish(event.id)


async def run_once(batch: int | None = None) -> int:
    """Process one batch of due events; returns how many were handled."""
    events = await claim(batch or get_settings().worker_batch_size)
    for e in events:
        await process(e)
    return len(events)


async def drain(max_rounds: int = 50) -> int:
    """Process until no due events remain (tests, scripts)."""
    total = 0
    for _ in range(max_rounds):
        n = await run_once()
        total += n
        if n == 0:
            break
    return total


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    s.validate_for_production()
    from app.main import check_db_role

    await check_db_role()
    log.info("worker started", extra={"data": {"handlers": sorted(HANDLERS)}})
    while not stop.is_set():
        try:
            n = await run_once()
        except Exception:  # noqa: BLE001 - keep the loop alive across DB blips
            log.exception("worker loop error")
            n = 0
        if n == 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=s.worker_poll_interval_seconds)
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())

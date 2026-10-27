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
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import configure_logging, org_id_var
from app.db.base import utcnow
from app.db.session import tenant_session
from app.models import Channel, Contact, Conversation, Event, KnowledgeSource, Message
from app.services.agent.runtime import run_reply
from app.services.channels import SendError, adapter_for
from app.services.knowledge import ingest_source

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
        await run_reply(db, org_id, uuid.UUID(payload["conversation_id"]), uuid.UUID(payload["message_id"]))
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


@handles("knowledge_source.created")
async def on_kb_source(org_id: uuid.UUID, payload: dict[str, Any]) -> None:
    async with tenant_session(org_id) as db:
        source = await db.get(KnowledgeSource, uuid.UUID(payload["source_id"]))
        if source is None:
            return
        await ingest_source(db, source)
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


async def process(event: Event) -> None:
    handler = HANDLERS.get(event.type)
    if handler is None:
        await finish(event.id)  # nothing subscribes to this event type (yet)
        return
    org_id_var.set(str(event.org_id) if event.org_id else None)
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

# Anvita API

Backend for **Anvita, the AI sales & marketing agent** ([web app](https://github.com/glalit52/anvita-sales-marketing-agent)).
It implements Phase 0 (foundations) and Phase 1 (the AI front desk) of the
[technical architecture](https://github.com/glalit52/anvita-sales-marketing-agent/blob/main/docs/04-technical-architecture.md):
multi-tenant workspaces, one customer graph, a unified inbox, the AI agent with tools and guardrails, booking,
payment links, and the public booking, pay and webchat surfaces.

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL 16 with row-level security · Alembic ·
Anthropic Claude (via the official SDK) · a transactional-outbox worker.

## Quick start

```bash
cp .env.example .env
docker compose up --build          # Postgres, migrations, API on :8000, worker
docker compose run --rm api python -m scripts.seed_demo   # optional: the "Smile Dental" demo workspace
open http://localhost:8000/docs    # interactive API docs (disabled in production)
```

Demo login: `priya@smiledental.in` / `demo-password-123`.

Without Docker:

```bash
uv venv -p 3.12 && uv pip install -e ".[dev]"
psql -U postgres -f deploy/postgres-init.sql   # creates the non-superuser role + databases
alembic upgrade head
uvicorn app.main:app --reload                  # API
python -m app.worker                           # worker (AI replies, outbound sends, knowledge ingestion)
pytest                                         # integration tests against anvita_test
```

With no `ANVITA_ANTHROPIC_API_KEY`, the agent runs on a deterministic offline model that answers from the
knowledge base or hands off to a human, so the whole product works locally without an API key.

## Architecture

```text
 web app / widget ──HTTPS──▶ FastAPI (app/api) ──▶ services (app/services) ──▶ PostgreSQL (RLS per org)
                                   │                        │
 WhatsApp / Razorpay ─webhook─▶ verify + store ──▶ events (outbox table) ──▶ worker (app/worker.py)
                                                                              ├─ agent runtime → Claude
                                                                              ├─ channel adapters (send)
                                                                              └─ knowledge ingestion
```

* **One customer graph.** Leads, patients, prospects and customers are all `contacts` moving through stages.
  Conversations, deals, appointments, payments, tasks and notes hang off the contact.
* **Tenant isolation, twice.** Every query filters by `org_id`, and Postgres RLS (`FORCE ROW LEVEL SECURITY`)
  enforces it again from `app.org_id`, which is set per transaction. The API refuses to start in production
  as a superuser or `BYPASSRLS` role.
* **Transactional outbox.** A domain change and the event it triggers commit together. The worker claims
  events with `FOR UPDATE SKIP LOCKED` (run as many workers as you like), leases them, and retries with backoff.
  Delivery is at least once, so every handler is idempotent.
* **Idempotency everywhere.** Provider message ids de-dupe webhook retries; idempotency keys cover outbound
  messages, AI bookings and online bookings. A Postgres exclusion constraint makes double-booking a provider
  impossible.

### The AI agent (`app/services/agent`)

For every inbound message (after a short debounce, because people send bursts):

1. **Should it speak?** Skips the turn if the thread is assigned to a human, a teammate replied recently
   (human-in-control pause), the channel's mode is `off`, or a newer message has arrived.
2. **Pre-checks.** Opt-out keywords (STOP, unsubscribe) write the consent ledger. Keyword and VIP
   escalation rules are applied.
3. **Classify** with the fast model (`claude-haiku-5-5`) using structured output: intent, sentiment, summary.
   Sentiment and intent rules can escalate here.
4. **Tool loop** with the conversation model (`claude-sonnet-5-5`, refusal fallbacks on). The system prompt
   has a byte-stable, prompt-cached part (persona, business profile, services, playbooks, guardrails) and a
   small volatile part (time, contact). Tools are strict-schema and tenant-scoped server-side:
   `search_knowledge`, `get_contact`, `update_contact`, `check_availability`, `book_appointment`,
   `cancel_appointment`, `create_payment_link` (capped per org), `create_task`, `handoff_to_human`.
5. **Deliver.** Autopilot sends the reply. Copilot stores a draft for a teammate to approve. An escalation
   reassigns the thread and notifies the team. Every run is recorded in `agent_runs` (tokens, cache hits,
   cost, tool calls, latency, outcome) and in the audit log.

The **sandbox** endpoint runs the same loop with real tools inside a transaction that is rolled back.

## API overview

All routes are under `/api/v1`. JSON is camelCase both ways, and query parameters accept camelCase too.
Errors look like `{"error": {"code", "message", "details"}}`. Money is integer minor units (paise) plus the
org currency. Timestamps are ISO 8601 UTC.

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/signup` `/auth/login` `/auth/refresh` (rotating, reuse-detecting) `/auth/logout` `/auth/switch-org` `/auth/accept-invite`, `GET /auth/me` |
| Workspace | `GET/PATCH /org`, `/locations`, `/team`, `/invites`, `GET /audit-log` |
| Contacts | `/contacts` (search, filters, sort, pagination), `/contacts/bulk`, `/{id}/notes`, `/{id}/consents`, `/{id}/timeline`, `/{id}/conversations` |
| Inbox | `/conversations` (views: all, mine, ai, escalated, unassigned), `/conversations/counts`, `/{id}/messages` (reply or internal note), `/{id}/drafts/{mid}/send`, `/{id}/ai-reply`, `PATCH /{id}` (status, assign, hand back to AI) |
| Tasks & pipeline | `/tasks` (groups: overdue, today, upcoming, done), `/deals`, `/deals/summary` |
| Calendar | `/services`, `/providers`, `/availability`, `/appointments` (book, reschedule, cancel) |
| Payments | `/payments`, `/payments/summary`, `/{id}/mark-paid`, `/{id}/cancel`, `/{id}/refund` |
| AI Agent | `/agent/config`, `/agent/publish`, `/agent/versions`, `/agent/knowledge` (+ `/faqs`, `/search`), `/agent/playbooks`, `/agent/escalation-rules`, `/agent/sandbox`, `/agent/runs`, `/agent/stats` |
| Operate | `/channels`, `/notifications`, `/dashboard` |
| Reviews | `/reviews` (+ `/stats`, `/sync`, `/{id}/draft`, `/{id}/reply`, `/settings`), `/review-requests` (+ `/funnel`) |
| Campaigns | `/segments` (+ `/preview`), `/templates` (+ `/submit`, `/status`), `/campaigns` (+ `/audience`, `/launch`, `/cancel`) |
| Sequences | `/sequences` (+ `/status`, `/enroll`, `/enrollments`, `/enrollments/{id}/stop`), `/sequences-meta/mailboxes` |
| Prospecting | `/prospecting/plan`, `/prospecting/sources`, `/prospecting/credits`, `/prospecting/searches` (+ `/run`, `/rows`, `/enrich`, `/columns`, `/add-to-crm`, `/export.csv`) |
| Marketing | `/marketing/ads/*` (campaigns, recommendations, copy), `/marketing/content` (+ `/ideas`, `/brief`, `/draft`, `/submit`), `/marketing/social` (+ `/captions`, `/schedule`), `/marketing/landing-pages` (+ `/rewrite`), `/marketing/aeo/*`, `/marketing/campaigns` (demand gen), `/marketing/approvals`, `/marketing/memory` |
| Automations | `/automations` (+ `/meta`, `/from-template/{id}`, `/{id}/toggle`, `/{id}/test`, `/runs`, `/events`) |
| Analytics | `/analytics/overview`, `/revenue-by-source`, `/revenue-weekly`, `/attribution?model=first|last|linear`, `/funnel`, `/ai`, `/team`, `/outbound` |
| Agent studio | `/agent/scenarios` (+ `/run`), `/agent/qa`, `/agent/knowledge-gaps` (+ `/answer`), `/agent/channel-modes` |
| Platform | `/integrations`, `/platform/providers`, `/api-keys`, `/webhook-endpoints`, `/webhook-events`, `/billing`, `/data-requests`, `/me/notification-preferences`, `/booking-settings`, `/onboarding/import-website` |
| Public | `/public/orgs/{slug}` (+ `/availability`, `/bookings`), `/public/pay/{token}`, `/public/webchat/...`, `/public/feedback/{token}` (+ `/rate`, `/private`, `/posted`), `/public/pages/{slug}/{page}` (+ `/submit`) |
| Webhooks | `/webhooks/whatsapp` (verify + signed inbound/status), `/webhooks/razorpay` (signed) |

See [docs/frontend-integration.md](docs/frontend-integration.md) for the page-by-page mapping to the web app.

## Configuration

Settings are environment variables prefixed `ANVITA_`; see [`app/core/config.py`](app/core/config.py) and
[`.env.example`](.env.example). In `staging` and `production` the app refuses to start without a strong
`JWT_SECRET` and an Anthropic API key, or with the test payment provider.

## Paid APIs and provider slots

Every third-party or paid API sits behind a small interface in [`app/services/providers`](app/services/providers):

| Kind | Used by | Slots (env vars) |
|---|---|---|
| `people_search` | Prospecting (decision-makers) | Apollo (`ANVITA_APOLLO_API_KEY`) |
| `enrichment` | Prospecting waterfall (email/phone) | Apollo, Hunter (tried in order) |
| `places` | Prospecting (business listings) | Google Places |
| `web_search` | Prospecting signals, AI columns | SerpAPI |
| `ads` | Ads copilot, analytics ad spend | Google Ads, Meta Ads |
| `reviews` | Review sync and replies | Google Business Profile |
| `search_console` | SEO content ideas | Google Search Console |
| `aeo` | Answer-engine visibility | Perplexity (add ChatGPT / Gemini the same way) |
| `social` | Social publishing | Meta, LinkedIn |
| `email`, `sms` | Channels | SMTP and Twilio (implemented); SendGrid, Exotel |
| `crm` | CRM sync | HubSpot |

To add or finish a vendor, implement the matching Protocol from `types.py` in `vendors.py` (each slot already
names the endpoint to call and links its docs), and `register()` it with the env vars it needs. Workspaces can
also paste their own keys through `POST /integrations/{id}/connect`; those win over platform env vars.
`GET /platform/providers` shows which slots are configured.

Outside production, deterministic **sample providers** stand in for every slot, so all screens and tests work
without keys (`ANVITA_ENABLE_SAMPLE_PROVIDERS=false` turns them off). In production an unconfigured slot answers
`501 provider_not_configured` with the vendor options in `details`, and a configured-but-unwritten slot
answers `501 provider_not_implemented` naming the call to implement. Credits are charged only for results.

## Operations

* **Processes:** one image runs three roles. The API is the default command, the worker is
  `python -m app.worker`, and migrations are `alembic upgrade head` (run it as a one-off job before rolling out).
* **Health:** `/healthz` (liveness) and `/readyz` (database reachable).
* **Logs:** JSON, one object per line, with `request_id` and `org_id` for correlation. The `x-request-id`
  header is honoured and echoed back.
* **Scaling:** the API is stateless. Run as many workers as you need. The in-process rate limiter is per
  replica, so move it to Redis before running several API replicas behind one budget.

## Roadmap (next phases)

* **Realtime:** SSE or WebSocket push for the inbox and webchat (today both poll).
* **Provider adapters:** fill in the remaining paid-API slots (Apollo, Google Places, Google/Meta Ads, Google
  Business Profile, Search Console, answer engines, social publishers, CRM sync). Each slot is listed by
  `GET /platform/providers` with its env vars and docs; see `app/services/providers/vendors.py`.
* **Voice agent** and telephony (missed-call webhooks feed the `missed` automation trigger).
* **Platform:** hybrid retrieval with pgvector, per-tenant KMS encryption of channel credentials, Redis rate
  limits, OpenTelemetry tracing.
